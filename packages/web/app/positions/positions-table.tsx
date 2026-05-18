"use client";

/**
 * PositionsTable — client island wrapping the shared shadcn DataTable.
 *
 * Open my_trades rows (opened_at set, closed_at NULL). Same canonical
 * instrument idiom as /trades and /leaders (DESIGN.md "Components →
 * Tables"): sticky header, right-aligned tabular numerics, sortable
 * headers with aria-sort on the <th>, row hover, one container radius.
 *
 * Column order is contractually Market, Leader, Side, Size, Entry,
 * Matched, Remaining, Fill, PnL, Opened — money is never misread.
 * The genuinely-empty dataset is handled by the parent Server Component.
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

function NumericCell({
  value,
  format,
}: {
  value: string | number | null;
  format: (n: number) => string;
}) {
  const n = toNum(value);
  return (
    <div className="text-right tabular-nums text-sm">
      {n === null ? (
        <span className="text-muted-foreground">Not recorded</span>
      ) : (
        format(n)
      )}
    </div>
  );
}

const columns: ColumnDef<MyTradeRow, unknown>[] = [
  {
    accessorKey: "market_id",
    header: "Market",
    cell: ({ getValue }) => {
      const v = getValue<string | null>();
      return v ? (
        <span
          className="inline-block max-w-[200px] truncate align-bottom font-mono text-xs"
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
    cell: ({ getValue }) => (
      <NumericCell
        value={getValue<string | null>()}
        format={(n) => n.toLocaleString("en-US")}
      />
    ),
    sortingFn: numericSort,
  },
  {
    accessorKey: "price",
    header: sortableHeader("Entry", { numeric: true }),
    cell: ({ getValue }) => (
      <NumericCell
        value={getValue<string | null>()}
        format={(n) => n.toFixed(4)}
      />
    ),
    sortingFn: numericSort,
  },
  {
    accessorKey: "matched_qty",
    header: sortableHeader("Matched", { numeric: true }),
    cell: ({ getValue }) => (
      <NumericCell
        value={getValue<string | null>()}
        format={(n) => n.toLocaleString("en-US")}
      />
    ),
    sortingFn: numericSort,
  },
  {
    accessorKey: "remaining_qty",
    header: sortableHeader("Remaining", { numeric: true }),
    cell: ({ getValue }) => (
      <NumericCell
        value={getValue<string | null>()}
        format={(n) => n.toLocaleString("en-US")}
      />
    ),
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
  {
    accessorKey: "opened_at",
    header: sortableHeader("Opened", { numeric: true }),
    cell: ({ getValue }) => (
      <div className="whitespace-nowrap text-right text-xs tabular-nums text-muted-foreground">
        {formatDateTime(getValue<Date | null>())}
      </div>
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
];

export function PositionsTable({ data }: { data: MyTradeRow[] }) {
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
