/**
 * /discovery — Candidate leader pool (Phase 1)
 *
 * Server Component: reads candidate_leaders via Drizzle, sorts/filters via
 * searchParams (all server-driven — no client state needed).
 */

import Link from "next/link";
import { desc, eq, gte, and } from "drizzle-orm";

import { db } from "@/lib/db";
import { candidateLeaders } from "@/lib/db/schema";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { ScoreBadge } from "@/components/score-badge";

// ---------------------------------------------------------------------------
// Types inferred from schema
// ---------------------------------------------------------------------------
type CandidateLeader = typeof candidateLeaders.$inferSelect;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function shortWallet(addr: string): string {
  return `${addr.slice(0, 6)}…${addr.slice(-4)}`;
}

function safeNum(v: string | null | undefined): number | null {
  if (v === null || v === undefined) return null;
  const n = Number(v);
  return isNaN(n) ? null : n;
}

const STATUS_OPTIONS = ["new", "watching", "following", "rejected"] as const;
const STRATEGY_OPTIONS = [
  "Scalper",
  "Day Trader",
  "Swing",
  "Position",
  "Hold-to-Resolve",
] as const;
const SORT_OPTIONS = ["total_score", "profit_score", "replicability_score"] as const;
type SortKey = (typeof SORT_OPTIONS)[number];

// ---------------------------------------------------------------------------
// Filter controls (rendered inline, server-driven via GET params)
// ---------------------------------------------------------------------------
function FilterBar({
  userStatus,
  strategyType,
  hasRiskFlag,
  minScore,
  sort,
}: {
  userStatus: string;
  strategyType: string;
  hasRiskFlag: string;
  minScore: string;
  sort: string;
}) {
  return (
    <form method="GET" className="flex flex-wrap gap-3 items-end">
      {/* Sort */}
      <label className="flex flex-col gap-1 text-xs font-medium text-muted-foreground">
        Sort by
        <select
          name="sort"
          defaultValue={sort}
          className="rounded border border-input bg-background px-2 py-1 text-sm"
        >
          {SORT_OPTIONS.map((s) => (
            <option key={s} value={s}>
              {s.replace(/_/g, " ")}
            </option>
          ))}
        </select>
      </label>

      {/* Status filter */}
      <label className="flex flex-col gap-1 text-xs font-medium text-muted-foreground">
        Status
        <select
          name="user_status"
          defaultValue={userStatus}
          className="rounded border border-input bg-background px-2 py-1 text-sm"
        >
          <option value="">All</option>
          {STATUS_OPTIONS.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
      </label>

      {/* Strategy filter */}
      <label className="flex flex-col gap-1 text-xs font-medium text-muted-foreground">
        Strategy
        <select
          name="strategy_type"
          defaultValue={strategyType}
          className="rounded border border-input bg-background px-2 py-1 text-sm"
        >
          <option value="">All</option>
          {STRATEGY_OPTIONS.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
      </label>

      {/* Risk flag filter */}
      <label className="flex flex-col gap-1 text-xs font-medium text-muted-foreground">
        Has risk flag
        <select
          name="has_risk_flag"
          defaultValue={hasRiskFlag}
          className="rounded border border-input bg-background px-2 py-1 text-sm"
        >
          <option value="">Any</option>
          <option value="yes">Yes</option>
          <option value="no">No</option>
        </select>
      </label>

      {/* Min score */}
      <label className="flex flex-col gap-1 text-xs font-medium text-muted-foreground">
        Min total score
        <input
          name="min_score"
          type="number"
          min="0"
          max="100"
          step="1"
          defaultValue={minScore}
          placeholder="0"
          className="w-20 rounded border border-input bg-background px-2 py-1 text-sm"
        />
      </label>

      <button
        type="submit"
        className="rounded bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground hover:bg-primary/90 transition-colors"
      >
        Apply
      </button>
      <a
        href="/discovery"
        className="rounded px-3 py-1.5 text-sm font-medium text-muted-foreground hover:text-foreground transition-colors"
      >
        Reset
      </a>
    </form>
  );
}

// ---------------------------------------------------------------------------
// Status badge variant map
// ---------------------------------------------------------------------------
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
// Page
// ---------------------------------------------------------------------------
interface PageProps {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}

export default async function DiscoveryPage({ searchParams }: PageProps) {
  const sp = await searchParams;

  // Parse search params
  const sort: SortKey = SORT_OPTIONS.includes(
    (sp.sort as SortKey) ?? ""
  )
    ? (sp.sort as SortKey)
    : "total_score";
  const userStatus =
    typeof sp.user_status === "string" ? sp.user_status : "";
  const strategyType =
    typeof sp.strategy_type === "string" ? sp.strategy_type : "";
  const hasRiskFlag =
    typeof sp.has_risk_flag === "string" ? sp.has_risk_flag : "";
  const minScoreRaw =
    typeof sp.min_score === "string" ? sp.min_score : "";
  const minScore = minScoreRaw !== "" ? Number(minScoreRaw) : null;

  // Build WHERE conditions
  const conditions = [];
  if (userStatus) {
    conditions.push(eq(candidateLeaders.user_status, userStatus));
  }
  if (strategyType) {
    conditions.push(eq(candidateLeaders.strategy_type, strategyType));
  }
  if (minScore !== null && !isNaN(minScore)) {
    // total_score is numeric (stored as string in JS) — compare using gte
    conditions.push(gte(candidateLeaders.total_score, String(minScore)));
  }
  // has_risk_flag filter applied post-fetch (array check is simpler in JS)

  // Sort column mapping
  const sortColMap = {
    total_score: candidateLeaders.total_score,
    profit_score: candidateLeaders.profit_score,
    replicability_score: candidateLeaders.replicability_score,
  } as const;

  // Fetch — TODO(verify): drizzle-orm 0.39 with postgres-js and Next.js 15 dynamic rendering
  const rows: CandidateLeader[] = await db
    .select()
    .from(candidateLeaders)
    .where(conditions.length ? and(...conditions) : undefined)
    .orderBy(desc(sortColMap[sort]));

  // Post-fetch: has_risk_flag filter
  const filtered =
    hasRiskFlag === "yes"
      ? rows.filter((r) => (r.risk_flags ?? []).length > 0)
      : hasRiskFlag === "no"
        ? rows.filter((r) => (r.risk_flags ?? []).length === 0)
        : rows;

  return (
    <div className="space-y-6">
      {/* Header */}
      <div>
        <h1 className="text-3xl font-bold">Discovery</h1>
        <p className="text-muted-foreground mt-1">
          Candidate leader pool — sorted by{" "}
          <code className="text-xs bg-muted px-1 rounded">{sort.replace(/_/g, " ")}</code> (desc)
        </p>
      </div>

      {/* Filters */}
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-base">Filters</CardTitle>
        </CardHeader>
        <CardContent>
          <FilterBar
            userStatus={userStatus}
            strategyType={strategyType}
            hasRiskFlag={hasRiskFlag}
            minScore={minScoreRaw}
            sort={sort}
          />
        </CardContent>
      </Card>

      {/* Table */}
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-base">
            Candidate Pool{" "}
            <span className="text-muted-foreground font-normal text-sm">
              ({filtered.length} leaders)
            </span>
          </CardTitle>
        </CardHeader>
        <CardContent className="p-0">
          {filtered.length === 0 ? (
            <EmptyState />
          ) : (
            <div className="overflow-x-auto">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead className="min-w-[140px]">Wallet</TableHead>
                    <TableHead>Name</TableHead>
                    <TableHead>Strategy</TableHead>
                    <TableHead className="text-right">Profit</TableHead>
                    <TableHead className="text-right">Replicability</TableHead>
                    <TableHead className="text-right">Total</TableHead>
                    <TableHead>Risk Flags</TableHead>
                    <TableHead>Status</TableHead>
                    <TableHead>Last Active</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {filtered.map((row) => (
                    <LeaderRow key={row.wallet_address} row={row} />
                  ))}
                </TableBody>
              </Table>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Row component
// ---------------------------------------------------------------------------
function LeaderRow({ row }: { row: CandidateLeader }) {
  const lastActive = row.last_active_at
    ? new Date(row.last_active_at).toLocaleDateString("en-CA") // YYYY-MM-DD
    : null;

  return (
    <TableRow className="hover:bg-muted/50">
      {/* Wallet (short + link) */}
      <TableCell className="font-mono text-xs">
        <Link
          href={`/discovery/${row.wallet_address}`}
          className="underline underline-offset-2 hover:text-primary"
          title={row.wallet_address}
        >
          {shortWallet(row.wallet_address)}
        </Link>
      </TableCell>

      {/* Display name */}
      <TableCell className="max-w-[140px] truncate text-sm">
        {row.display_name ?? (
          <span className="text-muted-foreground">—</span>
        )}
      </TableCell>

      {/* Strategy */}
      <TableCell className="text-sm">
        {row.strategy_type ?? (
          <span className="text-muted-foreground">—</span>
        )}
      </TableCell>

      {/* Profit score */}
      <TableCell className="text-right">
        <ScoreBadge label="" value={row.profit_score} />
      </TableCell>

      {/* Replicability score */}
      <TableCell className="text-right">
        <ScoreBadge label="" value={row.replicability_score} />
      </TableCell>

      {/* Total score */}
      <TableCell className="text-right">
        <ScoreBadge label="" value={row.total_score} />
      </TableCell>

      {/* Risk flags */}
      <TableCell>
        <RiskFlagCell flags={row.risk_flags} />
      </TableCell>

      {/* Status */}
      <TableCell>
        <Badge
          variant={STATUS_VARIANT[row.user_status] ?? "secondary"}
          className="capitalize text-xs"
        >
          {row.user_status}
        </Badge>
      </TableCell>

      {/* Last active */}
      <TableCell className="text-xs text-muted-foreground whitespace-nowrap">
        {lastActive ?? "—"}
      </TableCell>
    </TableRow>
  );
}

function RiskFlagCell({ flags }: { flags: string[] | null }) {
  if (!flags || flags.length === 0) {
    return <span className="text-xs text-muted-foreground">—</span>;
  }
  return (
    <div className="flex flex-wrap gap-1">
      {flags.map((f) => (
        <Badge key={f} variant="destructive" className="text-xs py-0 px-1.5">
          {f}
        </Badge>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Empty state
// ---------------------------------------------------------------------------
function EmptyState() {
  return (
    <div className="flex flex-col items-center justify-center gap-3 py-16 px-6 text-center">
      <div className="text-4xl">🔍</div>
      <h3 className="text-lg font-semibold">No candidates found</h3>
      <p className="text-sm text-muted-foreground max-w-sm">
        The discovery worker hasn&apos;t populated the candidate pool yet, or
        your current filters return no results. Run{" "}
        <code className="text-xs bg-muted px-1 rounded">
          polyecho discovery run --once
        </code>{" "}
        to seed data, or{" "}
        <a href="/discovery" className="underline hover:text-primary">
          reset filters
        </a>
        .
      </p>
    </div>
  );
}
