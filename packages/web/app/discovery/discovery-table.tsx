"use client";

/**
 * DiscoveryTable — client island wrapping shadcn data-table.
 * Receives server-fetched candidate_leaders rows as props.
 */

import Link from "next/link";
import { type ColumnDef, ArrowUpDown } from "@/components/ui/data-table";
import { DataTable } from "@/components/ui/data-table";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ScoreBadge } from "@/components/score-badge";
import type { CandidateLeaderRow } from "./page";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function shortWallet(addr: string): string {
  return `${addr.slice(0, 6)}…${addr.slice(-4)}`;
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
// Column definitions
// ---------------------------------------------------------------------------
const columns: ColumnDef<CandidateLeaderRow, unknown>[] = [
  {
    accessorKey: "wallet_address",
    header: "Wallet",
    cell: ({ row }) => (
      <Link
        href={`/discovery/${row.original.wallet_address}`}
        className="font-mono text-xs underline underline-offset-2 hover:text-primary"
        title={row.original.wallet_address}
      >
        {shortWallet(row.original.wallet_address)}
      </Link>
    ),
    enableHiding: false,
  },
  {
    accessorKey: "display_name",
    header: "Name",
    cell: ({ getValue }) => {
      const v = getValue<string | null>();
      return v ? (
        <span className="max-w-[140px] truncate text-sm">{v}</span>
      ) : (
        <span className="text-muted-foreground">—</span>
      );
    },
  },
  {
    accessorKey: "strategy_type",
    header: "Strategy",
    cell: ({ getValue }) => {
      const v = getValue<string | null>();
      return v ?? <span className="text-muted-foreground">—</span>;
    },
    filterFn: (row, id, filterValue: string) => {
      if (!filterValue) return true;
      return row.getValue<string | null>(id) === filterValue;
    },
  },
  {
    accessorKey: "profit_score",
    header: ({ column }) => (
      <Button
        variant="ghost"
        size="sm"
        className="-ml-3 h-8"
        onClick={() => column.toggleSorting(column.getIsSorted() === "asc")}
      >
        Profit
        <ArrowUpDown className="ml-1 h-3 w-3" />
      </Button>
    ),
    cell: ({ getValue }) => (
      <ScoreBadge label="" value={getValue<string | null>()} />
    ),
    sortingFn: (a, b, id) => {
      const av = Number(a.getValue<string | null>(id) ?? 0);
      const bv = Number(b.getValue<string | null>(id) ?? 0);
      return av - bv;
    },
  },
  {
    accessorKey: "replicability_score",
    header: ({ column }) => (
      <Button
        variant="ghost"
        size="sm"
        className="-ml-3 h-8"
        onClick={() => column.toggleSorting(column.getIsSorted() === "asc")}
      >
        Replicability
        <ArrowUpDown className="ml-1 h-3 w-3" />
      </Button>
    ),
    cell: ({ getValue }) => (
      <ScoreBadge label="" value={getValue<string | null>()} />
    ),
    sortingFn: (a, b, id) => {
      const av = Number(a.getValue<string | null>(id) ?? 0);
      const bv = Number(b.getValue<string | null>(id) ?? 0);
      return av - bv;
    },
  },
  {
    accessorKey: "total_score",
    header: ({ column }) => (
      <Button
        variant="ghost"
        size="sm"
        className="-ml-3 h-8"
        onClick={() => column.toggleSorting(column.getIsSorted() === "asc")}
      >
        Total
        <ArrowUpDown className="ml-1 h-3 w-3" />
      </Button>
    ),
    cell: ({ getValue }) => (
      <ScoreBadge label="" value={getValue<string | null>()} />
    ),
    sortingFn: (a, b, id) => {
      const av = Number(a.getValue<string | null>(id) ?? 0);
      const bv = Number(b.getValue<string | null>(id) ?? 0);
      return av - bv;
    },
  },
  {
    accessorKey: "risk_flags",
    header: "Risk Flags",
    cell: ({ getValue }) => {
      const flags = getValue<string[] | null>() ?? [];
      if (flags.length === 0) {
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
    },
    filterFn: (row, _id, filterValue: string) => {
      if (filterValue === "none") return filterValue === "none";
      const flags = row.getValue<string[] | null>("risk_flags") ?? [];
      if (filterValue === "yes") return flags.length > 0;
      if (filterValue === "no") return flags.length === 0;
      return true;
    },
    enableSorting: false,
  },
  {
    accessorKey: "user_status",
    header: "Status",
    cell: ({ getValue }) => {
      const v = getValue<string>();
      return (
        <Badge
          variant={STATUS_VARIANT[v] ?? "secondary"}
          className="capitalize text-xs"
        >
          {v}
        </Badge>
      );
    },
    filterFn: (row, id, filterValue: string) => {
      if (!filterValue) return true;
      return row.getValue<string>(id) === filterValue;
    },
  },
  {
    accessorKey: "last_active_at",
    header: "Last Active",
    cell: ({ getValue }) => {
      const v = getValue<Date | null>();
      if (!v) return <span className="text-xs text-muted-foreground">—</span>;
      return (
        <span className="text-xs text-muted-foreground whitespace-nowrap">
          {new Date(v).toLocaleDateString("en-CA")}
        </span>
      );
    },
    sortingFn: (a, b) => {
      const av = a.getValue<Date | null>("last_active_at");
      const bv = b.getValue<Date | null>("last_active_at");
      return (av ? new Date(av).getTime() : 0) - (bv ? new Date(bv).getTime() : 0);
    },
  },
];

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------
interface DiscoveryTableProps {
  data: CandidateLeaderRow[];
}

export function DiscoveryTable({ data }: DiscoveryTableProps) {
  return (
    <DataTable
      columns={columns}
      data={data}
      searchKey="wallet_address"
      searchPlaceholder="Filter by wallet…"
      facetedFilters={[
        {
          columnId: "user_status",
          title: "Status",
          options: [
            { label: "New", value: "new" },
            { label: "Watching", value: "watching" },
            { label: "Following", value: "following" },
            { label: "Rejected", value: "rejected" },
          ],
        },
        {
          columnId: "strategy_type",
          title: "Strategy",
          options: [
            { label: "Scalper", value: "Scalper" },
            { label: "Day Trader", value: "Day Trader" },
            { label: "Swing", value: "Swing" },
            { label: "Position", value: "Position" },
            { label: "Hold-to-Resolve", value: "Hold-to-Resolve" },
          ],
        },
        {
          columnId: "risk_flags",
          title: "Risk Flag",
          options: [
            { label: "Has flags", value: "yes" },
            { label: "No flags", value: "no" },
          ],
        },
      ]}
      pageSize={25}
    />
  );
}
