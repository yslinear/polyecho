"use client";

/**
 * DiscoveryTable — merged candidates-funnel client island (Phase-1 merge).
 *
 * One chip-aware table that replaces the legacy /discovery leaderboard AND
 * the legacy /research paper-trading ladder. The five chips along the top
 * (排行榜 / 驗證中 / 可升 live / 已退休 / 全部) write `?state=…`; the page-
 * level server component re-runs the right Drizzle query and hands the
 * island the filtered row set + the active state.
 *
 * Each chip carries its OWN column set + default sort. Switching chips
 * resets pagination to page 1 (RESEARCH_PAGE_SIZE 50/page).
 *
 *   排行榜    leader / lifecycle / total_score / profit_score /
 *             replicability_score / freshness  → sort total_score DESC
 *   驗證中    leader / lifecycle / copyability / paper_pnl / paper_days /
 *             paper_trades / gate              → sort copyability DESC
 *   可升 live  same as 驗證中 + promote-to-live ACTION
 *                                              → sort copyability DESC
 *   已退休    leader / cooldown_count / created_at
 *                                              → sort created_at DESC
 *   全部     leader / lifecycle / total_score / copyability / paper_pnl
 *                                              → sort total_score DESC
 *
 * The promote-to-live action is the existing /research Server Action,
 * unchanged (danger-passphrase InlineConfirm + paused follow_relations
 * insert). It is offered ONLY on the trial_ready chip (operator scope).
 *
 * Phase-1 explicitly does NOT touch user_status — no column, no filter, no
 * action. Schema/CLI cleanup is a separate commit.
 */

import * as React from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { toast } from "sonner";
import {
  ArrowDown,
  ArrowUp,
  ChevronLeft,
  ChevronRight,
  ChevronsUpDown,
  FlaskConical,
  ListFilter,
  Lock,
  MoreHorizontal,
  Search,
  Sliders,
  SlidersHorizontal,
  X,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Slider } from "@/components/ui/slider";
import { ScoreBadge } from "@/components/score-badge";
import { InlineConfirm } from "@/components/inline-confirm";
import { cn } from "@/lib/utils";
import { formatSignedUsd, pnlColor, toNum } from "@/lib/format";
import { t, type I18nKey } from "@/lib/i18n";
import { quickBacktest } from "./actions";
// The promote-to-live Server Action stays at /research/actions per the
// operator scope. Only this page.tsx redirects; the action lives on.
import { promoteToLiveFollow } from "@/app/research/actions";

// ---------------------------------------------------------------------------
// Public types
// ---------------------------------------------------------------------------
export type ChipState =
  | "discovered"
  | "active"
  | "trial_ready"
  | "retired"
  | "all";

/** One merged row — candidate_leaders LEFT JOIN paper_sessions. */
export interface DiscoveryRow {
  wallet_address: string;
  display_name: string | null;
  strategy_type: string | null;
  profit_score: string | null;
  replicability_score: string | null;
  total_score: string | null;
  risk_flags: string[] | null;
  last_seen_ts: Date | null;
  last_active_at: Date | null;
  lifecycle_state: string;
  locked: boolean;
  cooldown_count: number;
  cooldown_until: Date | null;
  first_discovered_at: Date | null;
  updated_at: Date | null;
  /** paper_sessions left-join — null when the leader has no open session. */
  paper_state: string | null;
  paper_started_at: Date | string | null;
  paper_trade_count: number | null;
  paper_copyable_pnl: string | number | null;
  paper_max_drawdown: string | number | null;
  paper_score: string | number | null;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function shortWallet(addr: string): string {
  return `${addr.slice(0, 6)}…${addr.slice(-4)}`;
}

function numericValue(v: unknown): number {
  const n = v === null || v === undefined ? NaN : Number(v);
  return Number.isNaN(n) ? -Infinity : n;
}

// Localise an enum DISPLAY only — the underlying value stays raw. An
// UNMAPPED value degrades to the raw value verbatim (lib/i18n.ts "degrade,
// don't crash"). The `t(`literal.prefix.${raw}`)` template forms below are
// deliberate: the i18n orphan-guard treats a STATIC template prefix as
// covering every leaf beneath it.
function strategyLabel(raw: string): string {
  const out = t(`discovery.table.strategy.${raw}` as I18nKey);
  return out === `discovery.table.strategy.${raw}` ? raw : out;
}
function lifecycleLabel(raw: string): string {
  const out = t(`discovery.table.lifecycle.${raw}` as I18nKey);
  return out === `discovery.table.lifecycle.${raw}` ? raw : out;
}

/** Whole-day age of a paper_session, or null when there is no session. */
function paperDays(startedAt: Date | string | null): number | null {
  if (!startedAt) return null;
  const start = startedAt instanceof Date ? startedAt : new Date(startedAt);
  if (Number.isNaN(start.getTime())) return null;
  const ms = Date.now() - start.getTime();
  return Math.max(0, Math.floor(ms / 86_400_000));
}

/** Coarse freshness pill — today / Nd / Nw / Nmo / 未知. Pure UTC math. */
function freshnessLabel(ts: Date | null): string {
  if (!ts) return t("discovery.table.lastSeen.never");
  const ms = Date.now() - new Date(ts).getTime();
  if (Number.isNaN(ms) || ms < 0) return t("discovery.table.lastSeen.today");
  const days = Math.floor(ms / 86_400_000);
  if (days < 1) return t("discovery.table.lastSeen.today");
  if (days < 7) return t("discovery.table.lastSeen.daysAgo", { days });
  if (days < 30) {
    return t("discovery.table.lastSeen.weeksAgo", { weeks: Math.floor(days / 7) });
  }
  return t("discovery.table.lastSeen.monthsAgo", { months: Math.floor(days / 30) });
}

const LIFECYCLE_VARIANT: Record<
  string,
  "default" | "secondary" | "outline" | "destructive"
> = {
  discovered: "secondary",
  candidate: "secondary",
  paper: "secondary",
  trial_ready: "default",
  cooldown: "outline",
  retired: "destructive",
};

// Strategy facet — the VALUE is the raw enum (matched against data + used
// by the column filter), the LABEL is localised so the faceted filter
// shows a zh-TW gloss while still filtering on the raw enum.
const STRATEGY_OPTIONS = [
  { label: strategyLabel("Scalper"), value: "Scalper" },
  { label: strategyLabel("Day Trader"), value: "Day Trader" },
  { label: strategyLabel("Swing"), value: "Swing" },
  { label: strategyLabel("Position"), value: "Position" },
  { label: strategyLabel("Hold-to-Resolve"), value: "Hold-to-Resolve" },
  { label: strategyLabel("Unknown"), value: "Unknown" },
] as const;

// Client-side pagination — RESEARCH_PAGE_SIZE 50/page (operator spec).
const PAGE_SIZE = 50;

// ---------------------------------------------------------------------------
// Gates that drive the paper-state "升級門檻進度" hint.
// ---------------------------------------------------------------------------
const GATE_TRADES = 10;
const GATE_DAYS = 7;

// ---------------------------------------------------------------------------
// Numeric range filter — inclusive [min, max] for 0–100 score columns.
// ---------------------------------------------------------------------------
type RangeFilter = [number | undefined, number | undefined];

function rowInRange(v: string | number | null, rf: RangeFilter): boolean {
  const [min, max] = rf;
  if (min === undefined && max === undefined) return true;
  const n = v === null ? NaN : Number(v);
  if (Number.isNaN(n)) return false;
  if (min !== undefined && n < min) return false;
  if (max !== undefined && n > max) return false;
  return true;
}

// ---------------------------------------------------------------------------
// Chip strip — five linked `<Link>`s that flip `?state=…`. Client component
// so the active styling is purely className-driven (no flicker mid-route).
// ---------------------------------------------------------------------------
const CHIPS: { state: ChipState; labelKey: I18nKey }[] = [
  { state: "discovered", labelKey: "discovery.list.chips.discovered" },
  { state: "active", labelKey: "discovery.list.chips.active" },
  { state: "trial_ready", labelKey: "discovery.list.chips.trialReady" },
  { state: "retired", labelKey: "discovery.list.chips.retired" },
  { state: "all", labelKey: "discovery.list.chips.all" },
];

function ChipStrip({
  active,
  trialReadyCount,
}: {
  active: ChipState;
  trialReadyCount: number;
}) {
  return (
    <nav
      aria-label={t("discovery.list.chips.ariaLabel")}
      className="flex flex-wrap gap-2"
    >
      {CHIPS.map(({ state, labelKey }) => {
        const isActive = state === active;
        const showBadge = state === "trial_ready" && trialReadyCount > 0;
        return (
          <Link
            key={state}
            href={`/discovery?state=${state}`}
            aria-current={isActive ? "page" : undefined}
            className={cn(
              "inline-flex h-8 items-center gap-1.5 rounded-full border px-3 text-xs font-medium transition-colors duration-150",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background",
              isActive
                ? "border-foreground bg-foreground text-background"
                : "border-border bg-background text-muted-foreground hover:border-foreground/40 hover:text-foreground",
            )}
          >
            <span>{t(labelKey)}</span>
            {showBadge && (
              <Badge
                variant="secondary"
                className="h-4 min-w-4 rounded-full px-1 text-[10px] tabular-nums"
                aria-label={t("discovery.list.trialReadyBadgeAria", {
                  count: trialReadyCount,
                })}
              >
                {trialReadyCount}
              </Badge>
            )}
          </Link>
        );
      })}
    </nav>
  );
}

// ---------------------------------------------------------------------------
// Sortable column header — ghost-button th with aria-sort on the parent.
// Mirrors the legacy ResearchTable SortableHead idiom.
// ---------------------------------------------------------------------------
type SortDir = "asc" | "desc";

interface SortableHeadProps {
  label: string;
  colKey: string;
  activeKey: string;
  dir: SortDir;
  onSort: (key: string) => void;
  align?: "left" | "right";
}

function SortableHead({
  label,
  colKey,
  activeKey,
  dir,
  onSort,
  align = "left",
}: SortableHeadProps) {
  const active = colKey === activeKey;
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
        onClick={() => onSort(colKey)}
      >
        {label}
        <Icon className="ml-1 h-3.5 w-3.5" aria-hidden="true" />
      </Button>
    </TableHead>
  );
}

// ---------------------------------------------------------------------------
// Faceted filter dropdown (single-select checkbox list, shadcn pattern).
// Reused for the strategy + risk-flag facets on the 排行榜 / 全部 chips.
// ---------------------------------------------------------------------------
interface FacetedFilterProps {
  title: string;
  value: string;
  options: readonly { label: string; value: string }[];
  onChange: (next: string) => void;
}

function FacetedFilter({ title, value, options, onChange }: FacetedFilterProps) {
  const activeOpt = options.find((o) => o.value === value);
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="outline" size="sm" className="h-8 text-xs">
          <ListFilter className="mr-1 h-3.5 w-3.5" aria-hidden="true" />
          {title}
          {activeOpt && (
            <Badge
              variant="secondary"
              className="ml-2 rounded-sm px-1 py-0 text-[10px] font-normal"
            >
              {activeOpt.label}
            </Badge>
          )}
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="w-44">
        <DropdownMenuLabel className="text-xs">{title}</DropdownMenuLabel>
        <DropdownMenuSeparator />
        <DropdownMenuCheckboxItem
          checked={!value}
          onCheckedChange={() => onChange("")}
        >
          {t("discovery.table.filters.all")}
        </DropdownMenuCheckboxItem>
        {options.map((opt) => (
          <DropdownMenuCheckboxItem
            key={opt.value}
            checked={value === opt.value}
            onCheckedChange={() =>
              onChange(value === opt.value ? "" : opt.value)
            }
          >
            {opt.label}
          </DropdownMenuCheckboxItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

// ---------------------------------------------------------------------------
// Numeric range filter — dual-thumb slider (0–100) inside a dropdown.
// Wired to a column's [min,max] via onChange; full span = no filter.
// ---------------------------------------------------------------------------
const RANGE_MIN = 0;
const RANGE_MAX = 100;
const RANGE_STEP = 1;

interface RangeFilterControlProps {
  title: string;
  value: RangeFilter;
  onChange: (next: RangeFilter) => void;
}

function RangeFilterControl({ title, value, onChange }: RangeFilterControlProps) {
  const [min, max] = value;
  const active = min !== undefined || max !== undefined;
  const lo = min ?? RANGE_MIN;
  const hi = max ?? RANGE_MAX;
  const unbounded = t("discovery.table.range.unbounded");
  const loText = min === undefined ? unbounded : String(lo);
  const hiText = max === undefined ? unbounded : String(hi);

  function handleValueChange(next: number[]) {
    const [nLo, nHi] = next;
    onChange([
      nLo <= RANGE_MIN ? undefined : nLo,
      nHi >= RANGE_MAX ? undefined : nHi,
    ]);
  }

  return (
    <DropdownMenu modal={false}>
      <DropdownMenuTrigger asChild>
        <Button variant="outline" size="sm" className="h-8 text-xs">
          <SlidersHorizontal className="mr-1 h-3.5 w-3.5" aria-hidden="true" />
          {title}
          {active && (
            <Badge
              variant="secondary"
              className="ml-2 rounded-sm px-1 py-0 text-[10px] font-normal tabular-nums"
            >
              {t("discovery.table.range.badge", { min: loText, max: hiText })}
            </Badge>
          )}
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="w-64">
        <DropdownMenuLabel className="text-xs">{title}</DropdownMenuLabel>
        <DropdownMenuSeparator />
        <div
          className="space-y-3 p-3"
          onKeyDown={(e) => e.stopPropagation()}
        >
          <div className="flex items-center justify-between text-xs tabular-nums text-muted-foreground">
            <span>{t("discovery.table.range.min")}</span>
            <span aria-live="polite">
              {t("discovery.table.range.current", { min: loText, max: hiText })}
            </span>
            <span>{t("discovery.table.range.max")}</span>
          </div>
          <Slider
            min={RANGE_MIN}
            max={RANGE_MAX}
            step={RANGE_STEP}
            value={[lo, hi]}
            onValueChange={handleValueChange}
            aria-label={t("discovery.table.range.sliderAria", { title })}
          />
        </div>
        <DropdownMenuSeparator />
        <Button
          variant="ghost"
          size="sm"
          className="h-8 w-full justify-start px-2 text-xs"
          disabled={!active}
          onClick={() => onChange([undefined, undefined])}
        >
          <X className="mr-1 h-3.5 w-3.5" aria-hidden="true" />
          {t("discovery.table.range.clear")}
        </Button>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

// ---------------------------------------------------------------------------
// Per-row quick-actions menu — kept across every chip (operator can run a
// backtest on any candidate). user_status actions are DROPPED in Phase-1.
// ---------------------------------------------------------------------------
function RowActions({ wallet }: { wallet: string }) {
  const router = useRouter();
  const [pending, setPending] = React.useState(false);

  async function onQuickBacktest() {
    setPending(true);
    try {
      const { id } = await quickBacktest(wallet);
      toast.success(t("discovery.table.toast.backtestQueued"));
      router.push(`/backtest/${id}`);
    } catch {
      toast.error(t("discovery.table.toast.backtestFailed"));
      setPending(false);
    }
  }

  const stop = (e: React.SyntheticEvent) => e.stopPropagation();

  return (
    <div onClick={stop} onKeyDown={stop}>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button
            variant="ghost"
            size="icon"
            className="h-7 w-7"
            aria-label={t("discovery.table.rowActionsAria", { wallet })}
            disabled={pending}
            onClick={stop}
          >
            <MoreHorizontal className="h-4 w-4" aria-hidden="true" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="w-56">
          <DropdownMenuLabel className="text-xs">
            {t("discovery.table.actions.label")}
          </DropdownMenuLabel>
          <DropdownMenuSeparator />
          <DropdownMenuItem
            className="text-xs"
            disabled={pending}
            onSelect={(e) => {
              e.preventDefault();
              void onQuickBacktest();
            }}
          >
            <FlaskConical className="mr-2 h-3.5 w-3.5" aria-hidden="true" />
            {t("discovery.table.actions.quickBacktest")}
          </DropdownMenuItem>
          <DropdownMenuItem
            className="text-xs"
            onSelect={() => router.push(`/backtest/new?leader=${wallet}`)}
          >
            <Sliders className="mr-2 h-3.5 w-3.5" aria-hidden="true" />
            {t("discovery.table.actions.customizeBacktest")}
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Promote-to-live action cell — offered ONLY on a trial_ready (unlocked)
// row. Mirrors the legacy /research PromoteAction exactly: danger-passphrase
// InlineConfirm + paused follow_relations insert (the Server Action stays
// at app/research/actions.ts).
// ---------------------------------------------------------------------------
function PromoteAction({ row }: { row: DiscoveryRow }) {
  if (row.locked) {
    return (
      <span className="inline-flex items-center gap-1 text-xs text-muted-foreground">
        <Lock className="h-3 w-3" aria-hidden="true" />
        {t("discovery.table.promote.locked")}
      </span>
    );
  }
  if (row.lifecycle_state !== "trial_ready") {
    return (
      <span className="text-xs text-muted-foreground">
        {t("discovery.table.promote.notReady")}
      </span>
    );
  }
  const stop = (e: React.SyntheticEvent) => e.stopPropagation();
  return (
    <div className="flex justify-end" onClick={stop} onKeyDown={stop}>
      <InlineConfirm
        title={t("discovery.table.promote.title")}
        description={t("discovery.table.promote.description")}
        requirePassphrase
        triggerVariant="danger"
        triggerClassName="h-8 gap-1.5 text-xs"
        successToast={t("discovery.table.promote.successToast")}
        action={async ({ confirmText, passphrase }) => {
          try {
            return await promoteToLiveFollow({
              leaderAddress: row.wallet_address,
              confirmText,
              passphrase,
            });
          } catch {
            return {
              ok: false,
              message: t("discovery.table.promote.genericError"),
            };
          }
        }}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Gate-progress hint — same logic as the legacy ResearchTable GateProgress.
// ---------------------------------------------------------------------------
function GateProgress({ row }: { row: DiscoveryRow }) {
  if (row.paper_state == null) {
    return (
      <span className="text-muted-foreground">
        {t("discovery.table.gate.none")}
      </span>
    );
  }
  const trades = row.paper_trade_count ?? 0;
  const days = paperDays(row.paper_started_at) ?? 0;
  const tradesMet = trades >= GATE_TRADES;
  const daysMet = days >= GATE_DAYS;

  if (tradesMet && daysMet) {
    return (
      <span className="text-muted-foreground">
        {t("discovery.table.gate.ready")}
      </span>
    );
  }
  return (
    <span className="text-muted-foreground">
      {!tradesMet
        ? t("discovery.table.gate.trades", { count: trades, need: GATE_TRADES })
        : t("discovery.table.gate.days", { day: days, need: GATE_DAYS })}
    </span>
  );
}

// ---------------------------------------------------------------------------
// Chip-aware column / sort spec.
//
// `key` is a stable column id used by SortableHead, the default-sort lookup
// and the empty-state colSpan. The legacy /research SortKey union is
// reused conceptually but extended to cover the leaderboard columns too.
// ---------------------------------------------------------------------------
type ColKey =
  | "wallet"
  | "lifecycle"
  | "total"
  | "profit"
  | "replicability"
  | "freshness"
  | "copyability"
  | "paperPnl"
  | "paperDays"
  | "paperTrades"
  | "gate"
  | "cooldownCount"
  | "createdAt"
  | "actions";

interface ColumnSpec {
  key: ColKey;
  /** zh-TW header label. */
  label: string;
  sortable: boolean;
  align?: "left" | "right";
}

function columnsFor(chip: ChipState): ColumnSpec[] {
  // EVERY chip leads with the wallet (a sticky-left "anchor" column) and
  // ends with the per-row quick-actions menu. The middle columns are
  // chip-specific so the operator sees ONLY the signal relevant to that
  // bucket — DESIGN.md "no identical card grids" applied to columns.
  const wallet: ColumnSpec = {
    key: "wallet",
    label: t("discovery.table.columns.wallet"),
    sortable: false,
  };
  const actions: ColumnSpec = {
    key: "actions",
    label: t("discovery.table.columns.actions"),
    sortable: false,
    align: "right",
  };

  switch (chip) {
    case "discovered":
      return [
        wallet,
        { key: "lifecycle", label: t("discovery.table.columns.lifecycle"), sortable: false },
        { key: "total", label: t("discovery.table.columns.total"), sortable: true, align: "right" },
        { key: "profit", label: t("discovery.table.columns.profit"), sortable: true, align: "right" },
        { key: "replicability", label: t("discovery.table.columns.replicability"), sortable: true, align: "right" },
        { key: "freshness", label: t("discovery.table.columns.lastSeen"), sortable: true, align: "right" },
        actions,
      ];
    case "active":
      return [
        wallet,
        { key: "lifecycle", label: t("discovery.table.columns.lifecycle"), sortable: false },
        { key: "copyability", label: t("discovery.table.columns.copyability"), sortable: true, align: "right" },
        { key: "paperPnl", label: t("discovery.table.columns.paperPnl"), sortable: true, align: "right" },
        { key: "paperDays", label: t("discovery.table.columns.paperDays"), sortable: true, align: "right" },
        { key: "paperTrades", label: t("discovery.table.columns.paperTrades"), sortable: true, align: "right" },
        { key: "gate", label: t("discovery.table.columns.gate"), sortable: false },
        actions,
      ];
    case "trial_ready":
      return [
        wallet,
        { key: "lifecycle", label: t("discovery.table.columns.lifecycle"), sortable: false },
        { key: "copyability", label: t("discovery.table.columns.copyability"), sortable: true, align: "right" },
        { key: "paperPnl", label: t("discovery.table.columns.paperPnl"), sortable: true, align: "right" },
        { key: "paperDays", label: t("discovery.table.columns.paperDays"), sortable: true, align: "right" },
        { key: "paperTrades", label: t("discovery.table.columns.paperTrades"), sortable: true, align: "right" },
        { key: "gate", label: t("discovery.table.columns.gate"), sortable: false },
        // The promote-to-live ACTION column — the bridge to real money.
        { key: "actions", label: t("discovery.table.promote.title"), sortable: false, align: "right" },
      ];
    case "retired":
      return [
        wallet,
        { key: "cooldownCount", label: t("discovery.table.columns.cooldownCount"), sortable: true, align: "right" },
        { key: "createdAt", label: t("discovery.table.columns.createdAt"), sortable: true, align: "right" },
        actions,
      ];
    case "all":
      return [
        wallet,
        { key: "lifecycle", label: t("discovery.table.columns.lifecycle"), sortable: false },
        { key: "total", label: t("discovery.table.columns.total"), sortable: true, align: "right" },
        { key: "copyability", label: t("discovery.table.columns.copyability"), sortable: true, align: "right" },
        { key: "paperPnl", label: t("discovery.table.columns.paperPnl"), sortable: true, align: "right" },
        actions,
      ];
  }
}

/** Default sort per chip — applied on chip change AND on initial mount. */
function defaultSortFor(chip: ChipState): { key: ColKey; dir: SortDir } {
  switch (chip) {
    case "discovered":
      return { key: "total", dir: "desc" };
    case "active":
    case "trial_ready":
      return { key: "copyability", dir: "desc" };
    case "retired":
      return { key: "createdAt", dir: "desc" };
    case "all":
      return { key: "total", dir: "desc" };
  }
}

/** Numeric/string projection of a row under a given sort key. */
function sortValue(row: DiscoveryRow, key: ColKey): number | string {
  switch (key) {
    case "wallet":
      return row.wallet_address;
    case "lifecycle":
      return row.lifecycle_state;
    case "total":
      return numericValue(row.total_score);
    case "profit":
      return numericValue(row.profit_score);
    case "replicability":
      return numericValue(row.replicability_score);
    case "freshness":
      return row.last_seen_ts
        ? new Date(row.last_seen_ts).getTime()
        : Number.NEGATIVE_INFINITY;
    case "copyability":
      return toNum(row.paper_score) ?? Number.NEGATIVE_INFINITY;
    case "paperPnl":
      return toNum(row.paper_copyable_pnl) ?? Number.NEGATIVE_INFINITY;
    case "paperDays":
      return paperDays(row.paper_started_at) ?? -1;
    case "paperTrades":
      return row.paper_trade_count ?? -1;
    case "cooldownCount":
      return row.cooldown_count;
    case "createdAt":
      return row.first_discovered_at
        ? new Date(row.first_discovered_at).getTime()
        : Number.NEGATIVE_INFINITY;
    case "gate":
    case "actions":
      return 0;
  }
}

/** Right-align flag for header + cell when align==="right". */
function isRight(c: ColumnSpec): boolean {
  return c.align === "right";
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------
interface DiscoveryTableProps {
  data: DiscoveryRow[];
  chip: ChipState;
  /** The 可升 live chip badge — count across the whole pool, not the page. */
  trialReadyCount: number;
}

export function DiscoveryTable({
  data,
  chip,
  trialReadyCount,
}: DiscoveryTableProps) {
  const router = useRouter();

  // ---------------------------------------------------------------------
  // Chip-aware sort. Switching chips resets pagination to page 1 AND
  // applies the new chip's default sort. We track the previously rendered
  // chip in a ref so a stale-sort state never leaks across navigations.
  // ---------------------------------------------------------------------
  const initial = defaultSortFor(chip);
  const [sortKey, setSortKey] = React.useState<ColKey>(initial.key);
  const [sortDir, setSortDir] = React.useState<SortDir>(initial.dir);
  const [page, setPage] = React.useState(1);
  const lastChipRef = React.useRef<ChipState>(chip);

  // When the chip changes, re-seed sort + reset page. `useEffect` runs
  // AFTER render; that is fine because the initial paint already used the
  // matching defaults (chip == lastChipRef.current on first mount).
  React.useEffect(() => {
    if (lastChipRef.current !== chip) {
      const d = defaultSortFor(chip);
      setSortKey(d.key);
      setSortDir(d.dir);
      setPage(1);
      lastChipRef.current = chip;
    }
  }, [chip]);

  // ---------------------------------------------------------------------
  // Toolbar filters — only used on the 排行榜 + 全部 chips (the leaderboard
  // surface). The ladder chips (驗證中 / 可升 live / 已退休) already filter
  // server-side on lifecycle_state, so the toolbar narrows on top of that
  // when the operator wants it (e.g. risk-flag inside 驗證中). DESIGN.md
  // "Restrained palette" — only show controls that change SOMETHING.
  // ---------------------------------------------------------------------
  const [globalFilter, setGlobalFilter] = React.useState("");
  const [strategyFilter, setStrategyFilter] = React.useState("");
  const [riskFilter, setRiskFilter] = React.useState("");
  const [profitRange, setProfitRange] = React.useState<RangeFilter>([
    undefined,
    undefined,
  ]);
  const [replRange, setReplRange] = React.useState<RangeFilter>([
    undefined,
    undefined,
  ]);
  const [totalRange, setTotalRange] = React.useState<RangeFilter>([
    undefined,
    undefined,
  ]);

  const isFiltered =
    globalFilter !== "" ||
    strategyFilter !== "" ||
    riskFilter !== "" ||
    profitRange[0] !== undefined ||
    profitRange[1] !== undefined ||
    replRange[0] !== undefined ||
    replRange[1] !== undefined ||
    totalRange[0] !== undefined ||
    totalRange[1] !== undefined;

  function resetFilters() {
    setGlobalFilter("");
    setStrategyFilter("");
    setRiskFilter("");
    setProfitRange([undefined, undefined]);
    setReplRange([undefined, undefined]);
    setTotalRange([undefined, undefined]);
  }

  // Score-range toolbar is shown only on the chips whose columns INCLUDE
  // the matching score (排行榜 + 全部).
  const showLeaderboardFilters = chip === "discovered" || chip === "all";
  const showRangeFilters = chip === "discovered" || chip === "all";

  // ---------------------------------------------------------------------
  // Filter pipeline
  // ---------------------------------------------------------------------
  const filteredRows = React.useMemo(() => {
    let rows = data;
    if (globalFilter) {
      const q = globalFilter.trim().toLowerCase();
      rows = rows.filter(
        (r) =>
          r.wallet_address.toLowerCase().includes(q) ||
          (r.display_name?.toLowerCase().includes(q) ?? false),
      );
    }
    if (showLeaderboardFilters && strategyFilter) {
      rows = rows.filter((r) => r.strategy_type === strategyFilter);
    }
    if (showLeaderboardFilters && riskFilter) {
      rows = rows.filter((r) => {
        const has = (r.risk_flags?.length ?? 0) > 0;
        return riskFilter === "yes" ? has : !has;
      });
    }
    if (showRangeFilters) {
      rows = rows.filter(
        (r) =>
          rowInRange(r.profit_score, profitRange) &&
          rowInRange(r.replicability_score, replRange) &&
          rowInRange(r.total_score, totalRange),
      );
    }
    return rows;
  }, [
    data,
    globalFilter,
    strategyFilter,
    riskFilter,
    profitRange,
    replRange,
    totalRange,
    showLeaderboardFilters,
    showRangeFilters,
  ]);

  // ---------------------------------------------------------------------
  // Sort
  // ---------------------------------------------------------------------
  const sortedRows = React.useMemo(() => {
    const copy = [...filteredRows];
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
  }, [filteredRows, sortKey, sortDir]);

  function onSort(key: string) {
    setPage(1);
    if (key === sortKey) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key as ColKey);
      // First click on a new column → desc (matches the legacy
      // ResearchTable expectation that high-value columns lead descending).
      setSortDir("desc");
    }
  }

  // ---------------------------------------------------------------------
  // Pagination
  // ---------------------------------------------------------------------
  const totalPages = Math.max(1, Math.ceil(sortedRows.length / PAGE_SIZE));
  const safePage = Math.min(page, totalPages);
  const pageRows = sortedRows.slice(
    (safePage - 1) * PAGE_SIZE,
    safePage * PAGE_SIZE,
  );
  const filteredCount = filteredRows.length;
  const firstRow = filteredCount === 0 ? 0 : (safePage - 1) * PAGE_SIZE + 1;
  const lastRow = Math.min(safePage * PAGE_SIZE, filteredCount);

  // Re-filter could shrink below safePage — snap back if so.
  React.useEffect(() => {
    if (safePage !== page) setPage(safePage);
  }, [safePage, page]);

  const columns = columnsFor(chip);

  // Chip-specific empty hint (different from "filter cleared everything").
  const chipEmptyHint =
    chip === "active"
      ? t("discovery.table.empty.noActive")
      : chip === "trial_ready"
        ? t("discovery.table.empty.noTrialReady")
        : chip === "retired"
          ? t("discovery.table.empty.noRetired")
          : t("discovery.table.empty.noDataTitle");

  // -------------------------------------------------------------------
  // Cell renderer — one chip-aware switch keeps each cell co-located
  // with its semantics (chip change reorders columns; the cell rules
  // are stable per column key).
  // -------------------------------------------------------------------
  function renderCell(col: ColumnSpec, row: DiscoveryRow): React.ReactNode {
    switch (col.key) {
      case "wallet":
        return (
          <span
            className="font-mono text-xs"
            title={row.wallet_address}
          >
            {shortWallet(row.wallet_address)}
          </span>
        );
      case "lifecycle":
        return (
          <Badge
            variant={LIFECYCLE_VARIANT[row.lifecycle_state] ?? "secondary"}
            className="text-xs"
          >
            {lifecycleLabel(row.lifecycle_state)}
          </Badge>
        );
      case "total":
        return <ScoreBadge label="" value={row.total_score} />;
      case "profit":
        return <ScoreBadge label="" value={row.profit_score} />;
      case "replicability":
        return <ScoreBadge label="" value={row.replicability_score} />;
      case "freshness":
        return (
          <span className="whitespace-nowrap text-xs tabular-nums text-muted-foreground">
            {freshnessLabel(row.last_seen_ts)}
          </span>
        );
      case "copyability":
        return <ScoreBadge label="" value={row.paper_score} />;
      case "paperPnl":
        return (
          <span
            className={cn(
              "whitespace-nowrap text-xs tabular-nums",
              row.paper_state ? pnlColor(row.paper_copyable_pnl) : "text-muted-foreground",
            )}
          >
            {row.paper_state
              ? formatSignedUsd(row.paper_copyable_pnl)
              : t("discovery.table.noSession")}
          </span>
        );
      case "paperDays": {
        const days = paperDays(row.paper_started_at);
        return (
          <span className="whitespace-nowrap text-xs tabular-nums text-muted-foreground">
            {days != null
              ? t("discovery.table.daysValue", { days })
              : t("discovery.table.noSession")}
          </span>
        );
      }
      case "paperTrades":
        return (
          <span className="whitespace-nowrap text-xs tabular-nums text-muted-foreground">
            {row.paper_state ? (row.paper_trade_count ?? 0) : t("discovery.table.noSession")}
          </span>
        );
      case "gate":
        return <GateProgress row={row} />;
      case "cooldownCount":
        return (
          <span className="text-xs tabular-nums text-muted-foreground">
            {row.cooldown_count}
          </span>
        );
      case "createdAt":
        return (
          <span className="whitespace-nowrap text-xs tabular-nums text-muted-foreground">
            {row.first_discovered_at
              ? new Date(row.first_discovered_at).toLocaleDateString("en-CA")
              : t("discovery.table.notSet")}
          </span>
        );
      case "actions":
        if (chip === "trial_ready") {
          return <PromoteAction row={row} />;
        }
        return (
          <div className="flex justify-end">
            <RowActions wallet={row.wallet_address} />
          </div>
        );
    }
  }

  return (
    <section className="space-y-5">
      <ChipStrip active={chip} trialReadyCount={trialReadyCount} />

      <h2 className="flex items-center gap-2 text-base font-semibold">
        {t("discovery.table.cardTitle")}
        <Badge
          variant="secondary"
          className="rounded-full tabular-nums font-medium"
        >
          {data.length}
        </Badge>
      </h2>

      {/* Toolbar — search applies on every chip; the strategy + risk +
          range facets are only meaningful on 排行榜 + 全部 (where those
          columns are visible). */}
      <div className="flex flex-wrap items-center gap-2">
        <div className="relative">
          <Search
            className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground"
            aria-hidden="true"
          />
          <Input
            placeholder={t("discovery.table.searchPlaceholder")}
            aria-label={t("discovery.table.searchAria")}
            value={globalFilter}
            onChange={(e) => {
              setGlobalFilter(e.target.value);
              setPage(1);
            }}
            className="h-8 w-[220px] pl-8"
          />
        </div>

        {showLeaderboardFilters && (
          <>
            <FacetedFilter
              title={t("discovery.table.filters.strategy")}
              value={strategyFilter}
              options={STRATEGY_OPTIONS}
              onChange={(v) => {
                setStrategyFilter(v);
                setPage(1);
              }}
            />
            <FacetedFilter
              title={t("discovery.table.filters.riskFlag")}
              value={riskFilter}
              options={[
                { label: t("discovery.table.filters.hasRiskFlag"), value: "yes" },
                { label: t("discovery.table.filters.noRiskFlag"), value: "no" },
              ]}
              onChange={(v) => {
                setRiskFilter(v);
                setPage(1);
              }}
            />
          </>
        )}

        {showRangeFilters && (
          <>
            <RangeFilterControl
              title={t("discovery.table.range.profit")}
              value={profitRange}
              onChange={(v) => {
                setProfitRange(v);
                setPage(1);
              }}
            />
            <RangeFilterControl
              title={t("discovery.table.range.replicability")}
              value={replRange}
              onChange={(v) => {
                setReplRange(v);
                setPage(1);
              }}
            />
            <RangeFilterControl
              title={t("discovery.table.range.total")}
              value={totalRange}
              onChange={(v) => {
                setTotalRange(v);
                setPage(1);
              }}
            />
          </>
        )}

        {isFiltered && (
          <Button
            variant="ghost"
            size="sm"
            className="h-8 px-2 text-xs"
            onClick={resetFilters}
          >
            {t("discovery.table.reset")}
            <X className="ml-1 h-3.5 w-3.5" aria-hidden="true" />
          </Button>
        )}
      </div>

      {/* Table */}
      <div className="max-h-[36rem] overflow-auto rounded-md border">
        <Table>
          <TableHeader className="sticky top-0 z-10 bg-muted">
            <TableRow className="hover:bg-transparent">
              {columns.map((c) =>
                c.sortable ? (
                  <SortableHead
                    key={c.key}
                    label={c.label}
                    colKey={c.key}
                    activeKey={sortKey}
                    dir={sortDir}
                    onSort={onSort}
                    align={c.align}
                  />
                ) : (
                  <TableHead
                    key={c.key}
                    className={isRight(c) ? "text-right" : undefined}
                  >
                    {c.key === "actions" && chip !== "trial_ready" ? (
                      <span className="sr-only">{c.label}</span>
                    ) : (
                      c.label
                    )}
                  </TableHead>
                ),
              )}
            </TableRow>
          </TableHeader>
          <TableBody>
            {pageRows.length ? (
              pageRows.map((row) => (
                <TableRow
                  key={row.wallet_address}
                  tabIndex={0}
                  aria-label={`View leader ${row.wallet_address}`}
                  className="cursor-pointer outline-none hover:bg-muted/50 focus-visible:bg-muted/50 focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset"
                  onClick={() => router.push(`/discovery/${row.wallet_address}`)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      router.push(`/discovery/${row.wallet_address}`);
                    }
                  }}
                >
                  {columns.map((c) => (
                    <TableCell
                      key={c.key}
                      className={cn(
                        "py-2.5",
                        isRight(c) && "text-right tabular-nums",
                      )}
                    >
                      {renderCell(c, row)}
                    </TableCell>
                  ))}
                </TableRow>
              ))
            ) : (
              <TableRow className="hover:bg-transparent">
                <TableCell
                  colSpan={columns.length}
                  className="h-40 text-center"
                >
                  <div className="mx-auto flex max-w-sm flex-col items-center justify-center gap-2">
                    {isFiltered ? (
                      <>
                        <p className="text-sm font-medium text-foreground">
                          {t("discovery.table.empty.filteredTitle")}
                        </p>
                        <p className="text-sm text-muted-foreground">
                          {t("discovery.table.empty.filteredHint")}
                        </p>
                        <Button
                          variant="outline"
                          size="sm"
                          className="mt-3 h-8 text-xs"
                          onClick={resetFilters}
                        >
                          {t("discovery.table.empty.clearFilters")}
                        </Button>
                      </>
                    ) : (
                      <>
                        <p className="text-sm font-medium text-foreground">
                          {chipEmptyHint}
                        </p>
                        <p className="text-sm text-muted-foreground">
                          {t("discovery.table.empty.noDataHint")}
                        </p>
                      </>
                    )}
                  </div>
                </TableCell>
              </TableRow>
            )}
          </TableBody>
        </Table>
      </div>

      {/* Pagination footer — 50/page (RESEARCH_PAGE_SIZE). */}
      <div className="flex flex-wrap items-center justify-between gap-3 pt-1 text-sm text-muted-foreground">
        <span className="tabular-nums">
          {filteredCount === 0
            ? t("discovery.table.pagination.empty")
            : t("discovery.table.pagination.range", {
                first: firstRow,
                last: lastRow,
                total: filteredCount,
              })}
        </span>
        <div className="flex flex-wrap items-center gap-4">
          <span className="min-w-[5.5rem] text-center text-xs tabular-nums">
            {t("discovery.table.pagination.pageOf", {
              page: safePage,
              total: totalPages,
            })}
          </span>
          <div className="flex items-center gap-1">
            <Button
              variant="outline"
              size="icon"
              className="h-7 w-7"
              aria-label={t("discovery.table.pagination.previousPage")}
              onClick={() => setPage((p) => Math.max(1, p - 1))}
              disabled={safePage <= 1}
            >
              <ChevronLeft className="h-4 w-4" aria-hidden="true" />
            </Button>
            <Button
              variant="outline"
              size="icon"
              className="h-7 w-7"
              aria-label={t("discovery.table.pagination.nextPage")}
              onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
              disabled={safePage >= totalPages}
            >
              <ChevronRight className="h-4 w-4" aria-hidden="true" />
            </Button>
          </div>
        </div>
      </div>
    </section>
  );
}
