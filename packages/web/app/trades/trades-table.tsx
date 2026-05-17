"use client";

/**
 * TradesTable — client island wrapping the shadcn data-table.
 * Receives server-fetched my_trades rows as props. Mirrors the
 * discovery-table pattern (sortable headers, faceted filter, pagination).
 */

import { type ColumnDef, ArrowUpDown } from "@/components/ui/data-table";
import { DataTable } from "@/components/ui/data-table";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  formatSignedUsd,
  pnlColor,
  shortAddress,
  formatDateTime,
  toNum,
} from "@/lib/format";
import type { MyTradeRow } from "./page";

const FILL_VARIANT: Record<
  string,
  "default" | "secondary" | "outline" | "destructive"
> = {
  filled: "default",
  pending: "secondary",
  partial: "outline",
  rejected: "destructive",
};

function sortableHeader(label: string) {
  const Header = ({
    column,
  }: {
    column: {
      toggleSorting: (desc?: boolean) => void;
      getIsSorted: () => false | "asc" | "desc";
    };
  }) => (
    <Button
      variant="ghost"
      size="sm"
      className="-ml-3 h-8"
      onClick={() => column.toggleSorting(column.getIsSorted() === "asc")}
    >
      {label}
      <ArrowUpDown className="ml-1 h-3 w-3" />
    </Button>
  );
  Header.displayName = `SortableHeader(${label})`;
  return Header;
}

function numericSort(a: { getValue: (id: string) => unknown }, b: { getValue: (id: string) => unknown }, id: string) {
  return (toNum(a.getValue(id) as string) ?? 0) - (toNum(b.getValue(id) as string) ?? 0);
}

const columns: ColumnDef<MyTradeRow, unknown>[] = [
  {
    accessorKey: "opened_at",
    header: sortableHeader("Opened"),
    cell: ({ getValue }) => (
      <span className="text-xs text-muted-foreground whitespace-nowrap">
        {formatDateTime(getValue<Date | null>())}
      </span>
    ),
    sortingFn: (a, b) => {
      const av = a.getValue<Date | null>("opened_at");
      const bv = b.getValue<Date | null>("opened_at");
      return (
        (av ? new Date(av).getTime() : 0) - (bv ? new Date(bv).getTime() : 0)
      );
    },
    enableHiding: false,
  },
  {
    accessorKey: "leader_address",
    header: "Leader",
    cell: ({ getValue }) => {
      const v = getValue<string | null>();
      return v ? (
        <span className="font-mono text-xs" title={v}>
          {shortAddress(v)}
        </span>
      ) : (
        <span className="text-muted-foreground">—</span>
      );
    },
  },
  {
    accessorKey: "market_id",
    header: "Market",
    cell: ({ getValue }) => {
      const v = getValue<string | null>();
      return v ? (
        <span
          className="font-mono text-xs max-w-[160px] inline-block truncate align-bottom"
          title={v}
        >
          {v}
        </span>
      ) : (
        <span className="text-muted-foreground">—</span>
      );
    },
  },
  {
    accessorKey: "side",
    header: "Side",
    cell: ({ getValue }) => {
      const v = getValue<string | null>();
      if (!v) return <span className="text-muted-foreground">—</span>;
      const buy = v.toUpperCase() === "BUY";
      return (
        <span
          className={
            buy
              ? "text-xs font-semibold text-green-600 dark:text-green-400"
              : "text-xs font-semibold text-red-600 dark:text-red-400"
          }
        >
          {v.toUpperCase()}
        </span>
      );
    },
  },
  {
    accessorKey: "size",
    header: sortableHeader("Size"),
    cell: ({ getValue }) => {
      const n = toNum(getValue<string | null>());
      return (
        <span className="tabular-nums text-sm">
          {n === null ? "—" : n.toLocaleString("en-US")}
        </span>
      );
    },
    sortingFn: numericSort,
  },
  {
    accessorKey: "price",
    header: sortableHeader("Price"),
    cell: ({ getValue }) => {
      const n = toNum(getValue<string | null>());
      return (
        <span className="tabular-nums text-sm">
          {n === null ? "—" : n.toFixed(4)}
        </span>
      );
    },
    sortingFn: numericSort,
  },
  {
    accessorKey: "fill_status",
    header: "Fill",
    cell: ({ getValue }) => {
      const v = getValue<string>();
      return (
        <Badge
          variant={FILL_VARIANT[v] ?? "secondary"}
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
    accessorKey: "pnl_usd",
    header: sortableHeader("PnL"),
    cell: ({ getValue }) => {
      const v = getValue<string | null>();
      if (toNum(v) === null) {
        return <span className="text-xs text-muted-foreground">—</span>;
      }
      return (
        <span className={`tabular-nums text-sm font-medium ${pnlColor(v)}`}>
          {formatSignedUsd(v)}
        </span>
      );
    },
    sortingFn: numericSort,
  },
];

interface TradesTableProps {
  data: MyTradeRow[];
}

export function TradesTable({ data }: TradesTableProps) {
  return (
    <DataTable
      columns={columns}
      data={data}
      searchKey="market_id"
      searchPlaceholder="Filter by market…"
      facetedFilters={[
        {
          columnId: "fill_status",
          title: "Fill Status",
          options: [
            { label: "Filled", value: "filled" },
            { label: "Pending", value: "pending" },
            { label: "Partial", value: "partial" },
            { label: "Rejected", value: "rejected" },
          ],
        },
      ]}
      pageSize={25}
    />
  );
}
