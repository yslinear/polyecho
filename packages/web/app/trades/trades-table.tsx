"use client";

/**
 * TradesTable — client island wrapping the shared shadcn DataTable.
 *
 * Receives server-fetched my_trades rows as props. This is the canonical
 * instrument table idiom (DESIGN.md "Components → Tables"): sticky header,
 * right-aligned tabular numerics, sortable headers with aria-sort on the
 * <th>, row hover, one container radius. /positions and /leaders use the
 * exact same idiom so the four read as one instrument.
 *
 * The genuinely-empty dataset is handled by the parent Server Component
 * (honest copy + the real next action). The DataTable's own fallback only
 * shows when rows EXIST but a filter excludes them all — that is a true
 * "no matches" state, not an empty page.
 */

import { type ColumnDef, ArrowUpDown } from "@/components/ui/data-table";
import { DataTable } from "@/components/ui/data-table";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Pnl } from "@/components/pnl";
import { shortAddress, formatDateTime, toNum } from "@/lib/format";
import { cn } from "@/lib/utils";
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

/**
 * Sortable header. `numeric` right-aligns the header to sit over a
 * right-aligned numeric column (money legibility — a misread number is a
 * bug). aria-sort is mirrored onto the cell from TanStack sort state.
 */
function sortableHeader(label: string, opts?: { numeric?: boolean }) {
  const Header = ({
    column,
  }: {
    column: {
      toggleSorting: (desc?: boolean) => void;
      getIsSorted: () => false | "asc" | "desc";
    };
  }) => {
    const sorted = column.getIsSorted();
    return (
      <div
        className={cn("flex", opts?.numeric && "justify-end")}
        aria-sort={
          sorted === "asc"
            ? "ascending"
            : sorted === "desc"
              ? "descending"
              : "none"
        }
      >
        <Button
          variant="ghost"
          size="sm"
          className="-ml-3 h-8 data-[numeric=true]:-mr-3 data-[numeric=true]:ml-0"
          data-numeric={opts?.numeric ? "true" : undefined}
          onClick={() => column.toggleSorting(sorted === "asc")}
        >
          {label}
          <ArrowUpDown className="ml-1 h-3 w-3" />
        </Button>
      </div>
    );
  };
  Header.displayName = `SortableHeader(${label})`;
  return Header;
}

function numericSort(
  a: { getValue: (id: string) => unknown },
  b: { getValue: (id: string) => unknown },
  id: string
) {
  return (
    (toNum(a.getValue(id) as string) ?? 0) -
    (toNum(b.getValue(id) as string) ?? 0)
  );
}

const columns: ColumnDef<MyTradeRow, unknown>[] = [
  {
    accessorKey: "opened_at",
    header: sortableHeader("Opened"),
    cell: ({ getValue }) => (
      <span className="whitespace-nowrap text-xs tabular-nums text-muted-foreground">
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
        <span className="text-muted-foreground">Not set</span>
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
          className="inline-block max-w-[160px] truncate align-bottom font-mono text-xs"
          title={v}
        >
          {v}
        </span>
      ) : (
        <span className="text-muted-foreground">Not set</span>
      );
    },
  },
  {
    accessorKey: "side",
    header: "Side",
    cell: ({ getValue }) => {
      const v = getValue<string | null>();
      if (!v) return <span className="text-muted-foreground">Not set</span>;
      const buy = v.toUpperCase() === "BUY";
      return (
        <Badge variant={buy ? "default" : "secondary"} className="text-xs">
          {v.toUpperCase()}
        </Badge>
      );
    },
  },
  {
    accessorKey: "size",
    header: sortableHeader("Size", { numeric: true }),
    cell: ({ getValue }) => {
      const n = toNum(getValue<string | null>());
      return (
        <div className="text-right tabular-nums text-sm">
          {n === null ? (
            <span className="text-muted-foreground">Not recorded</span>
          ) : (
            n.toLocaleString("en-US")
          )}
        </div>
      );
    },
    sortingFn: numericSort,
  },
  {
    accessorKey: "price",
    header: sortableHeader("Price", { numeric: true }),
    cell: ({ getValue }) => {
      const n = toNum(getValue<string | null>());
      return (
        <div className="text-right tabular-nums text-sm">
          {n === null ? (
            <span className="text-muted-foreground">Not recorded</span>
          ) : (
            n.toFixed(4)
          )}
        </div>
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
          className="text-xs capitalize"
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
    header: sortableHeader("PnL", { numeric: true }),
    cell: ({ getValue }) => {
      const v = getValue<string | null>();
      if (toNum(v) === null) {
        return (
          <div className="text-right text-sm text-muted-foreground">
            Not recorded
          </div>
        );
      }
      return (
        <div className="text-right">
          <Pnl value={v} className="text-sm" />
        </div>
      );
    },
    sortingFn: numericSort,
  },
];

interface TradesTableProps {
  data: MyTradeRow[];
}

export function TradesTable({ data }: TradesTableProps) {
  // Genuinely-empty dataset: one calm sentence + the real next action
  // (DESIGN.md "real empty state"). The DataTable toolbar/pagination would
  // be noise with nothing to act on, so the island owns this state — the
  // same idiom /positions and /leaders use.
  if (data.length === 0) {
    return (
      <div className="rounded-lg border border-dashed py-16 text-center">
        <p className="text-sm text-muted-foreground">
          No copy trades recorded yet. They appear here once trader-core
          executes a copy for a followed leader.
        </p>
      </div>
    );
  }

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
