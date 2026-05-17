"use client";

/**
 * DiscoveryTable — purpose-built shadcn data-table for the candidate-leader
 * pool. Self-contained (does not use the shared `components/ui/data-table`
 * so this page can layer in a Card wrapper, page-size Select, column
 * visibility, reset, aria-sort headers and a rich empty state without
 * regressing the shared component consumed by /trades).
 *
 * Stack: @tanstack/react-table v8 + shadcn primitives + lucide icons.
 * Read-only; receives server-fetched candidate_leaders rows as props.
 */

import * as React from "react";
import { useRouter } from "next/navigation";
import { toast } from "sonner";
import {
  type ColumnDef,
  type ColumnFiltersState,
  type SortingState,
  type VisibilityState,
  flexRender,
  getCoreRowModel,
  getFilteredRowModel,
  getPaginationRowModel,
  getSortedRowModel,
  useReactTable,
} from "@tanstack/react-table";
import {
  ArrowDown,
  ArrowUp,
  ChevronLeft,
  ChevronRight,
  ChevronsUpDown,
  Eye,
  FlaskConical,
  ListFilter,
  MoreHorizontal,
  Search,
  SlidersHorizontal,
  Sliders,
  UserMinus,
  UserPlus,
  X,
} from "lucide-react";

import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
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
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { ScoreBadge } from "@/components/score-badge";
import { cn } from "@/lib/utils";
import { quickBacktest, updateLeaderStatus } from "./actions";
import type { LeaderStatus } from "./actions";
import type { CandidateLeaderRow } from "./page";

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

const STATUS_VARIANT: Record<
  string,
  "default" | "secondary" | "outline" | "destructive"
> = {
  new: "secondary",
  watching: "outline",
  following: "default",
  rejected: "destructive",
};

const STRATEGY_VARIANT: Record<
  string,
  "default" | "secondary" | "outline" | "destructive"
> = {
  Scalper: "default",
  "Day Trader": "secondary",
  Swing: "outline",
  Position: "outline",
  "Hold-to-Resolve": "secondary",
};

const STATUS_OPTIONS = [
  { label: "New", value: "new" },
  { label: "Watching", value: "watching" },
  { label: "Following", value: "following" },
  { label: "Rejected", value: "rejected" },
] as const;

const STRATEGY_OPTIONS = [
  { label: "Scalper", value: "Scalper" },
  { label: "Day Trader", value: "Day Trader" },
  { label: "Swing", value: "Swing" },
  { label: "Position", value: "Position" },
  { label: "Hold-to-Resolve", value: "Hold-to-Resolve" },
] as const;

const PAGE_SIZE_OPTIONS = [10, 25, 50] as const;

// Human labels for the column-visibility menu (column id -> label).
const COLUMN_LABELS: Record<string, string> = {
  wallet_address: "Wallet",
  display_name: "Name",
  strategy_type: "Strategy",
  profit_score: "Profit",
  replicability_score: "Replicability",
  total_score: "Total",
  risk_flags: "Risk Flags",
  user_status: "Status",
  last_active_at: "Last Active",
};

// ---------------------------------------------------------------------------
// Sortable header — ghost Button + trailing sort icon. aria-sort is applied
// on the parent <th> (see SortableHead below) for WCAG sortable tables.
// ---------------------------------------------------------------------------
function makeSortableHeader(label: string) {
  const Header = ({
    column,
  }: {
    column: {
      toggleSorting: (desc?: boolean) => void;
      getIsSorted: () => false | "asc" | "desc";
    };
  }) => {
    const sorted = column.getIsSorted();
    const Icon =
      sorted === "asc" ? ArrowUp : sorted === "desc" ? ArrowDown : ChevronsUpDown;
    return (
      <Button
        variant="ghost"
        size="sm"
        className="-ml-3 h-8 data-[active=true]:text-foreground"
        data-active={sorted !== false}
        onClick={() => column.toggleSorting(column.getIsSorted() === "asc")}
      >
        {label}
        <Icon className="ml-1 h-3.5 w-3.5" aria-hidden="true" />
      </Button>
    );
  };
  Header.displayName = `SortableHeader(${label})`;
  return Header;
}

// ---------------------------------------------------------------------------
// Per-row quick actions — PRD §5.7 "安全寫入" sanctioned writes.
// All clicks stopPropagation so the row→detail navigation does not fire.
// ---------------------------------------------------------------------------
const STATUS_ACTIONS: {
  status: LeaderStatus;
  label: string;
  Icon: typeof UserPlus;
  destructive?: boolean;
}[] = [
  { status: "watching", label: "Watch", Icon: Eye },
  { status: "following", label: "Follow", Icon: UserPlus },
  { status: "rejected", label: "Reject", Icon: UserMinus, destructive: true },
];

function RowActions({ row }: { row: CandidateLeaderRow }) {
  const router = useRouter();
  const [pending, setPending] = React.useState(false);
  const wallet = row.wallet_address;

  async function onQuickBacktest() {
    setPending(true);
    try {
      const { id } = await quickBacktest(wallet);
      toast.success("Backtest queued (90d · Balanced)");
      router.push(`/backtest/${id}`);
    } catch {
      toast.error("Failed to queue backtest");
      setPending(false);
    }
  }

  async function onSetStatus(status: LeaderStatus, label: string) {
    setPending(true);
    try {
      await updateLeaderStatus(wallet, status);
      toast.success(`Leader marked ${label.toLowerCase()}`);
      router.refresh();
    } catch {
      toast.error("Failed to update leader");
    } finally {
      setPending(false);
    }
  }

  // Stop row click/keyboard navigation from firing for any action interaction.
  const stop = (e: React.SyntheticEvent) => e.stopPropagation();

  return (
    <div onClick={stop} onKeyDown={stop}>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button
            variant="ghost"
            size="icon"
            className="h-7 w-7"
            aria-label={`Actions for ${wallet}`}
            disabled={pending}
            onClick={stop}
          >
            <MoreHorizontal className="h-4 w-4" aria-hidden="true" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="w-56">
          <DropdownMenuLabel className="text-xs">Actions</DropdownMenuLabel>
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
            Quick backtest (90d · Balanced)
          </DropdownMenuItem>
          <DropdownMenuItem
            className="text-xs"
            onSelect={() => router.push(`/backtest/new?leader=${wallet}`)}
          >
            <Sliders className="mr-2 h-3.5 w-3.5" aria-hidden="true" />
            Customize backtest…
          </DropdownMenuItem>
          <DropdownMenuSeparator />
          {STATUS_ACTIONS.filter((a) => a.status !== row.user_status).map(
            ({ status, label, Icon, destructive }) => (
              <DropdownMenuItem
                key={status}
                className={cn(
                  "text-xs",
                  destructive &&
                    "text-destructive focus:text-destructive"
                )}
                disabled={pending}
                onSelect={(e) => {
                  e.preventDefault();
                  void onSetStatus(status, label);
                }}
              >
                <Icon className="mr-2 h-3.5 w-3.5" aria-hidden="true" />
                {label}
              </DropdownMenuItem>
            )
          )}
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Column definitions
// ---------------------------------------------------------------------------
const columns: ColumnDef<CandidateLeaderRow, unknown>[] = [
  {
    accessorKey: "wallet_address",
    header: "Wallet",
    cell: ({ row }) => (
      <span
        className="font-mono text-xs"
        title={row.original.wallet_address}
      >
        {shortWallet(row.original.wallet_address)}
      </span>
    ),
    enableHiding: false,
    enableSorting: false,
  },
  {
    accessorKey: "display_name",
    header: "Name",
    cell: ({ getValue }) => {
      const v = getValue<string | null>();
      return v ? (
        <span className="block max-w-[160px] truncate text-sm font-medium">
          {v}
        </span>
      ) : (
        <span className="text-muted-foreground">—</span>
      );
    },
    enableSorting: false,
  },
  {
    accessorKey: "strategy_type",
    header: "Strategy",
    cell: ({ getValue }) => {
      const v = getValue<string | null>();
      if (!v) return <span className="text-muted-foreground">—</span>;
      return (
        <Badge variant={STRATEGY_VARIANT[v] ?? "outline"} className="text-xs">
          {v}
        </Badge>
      );
    },
    filterFn: (row, id, filterValue: string) => {
      if (!filterValue) return true;
      return row.getValue<string | null>(id) === filterValue;
    },
    enableSorting: false,
  },
  {
    accessorKey: "profit_score",
    header: makeSortableHeader("Profit"),
    cell: ({ getValue }) => (
      <div className="text-right tabular-nums">
        <ScoreBadge label="" value={getValue<string | null>()} />
      </div>
    ),
    sortingFn: (a, b, id) =>
      numericValue(a.getValue(id)) - numericValue(b.getValue(id)),
  },
  {
    accessorKey: "replicability_score",
    header: makeSortableHeader("Replicability"),
    cell: ({ getValue }) => (
      <div className="text-right tabular-nums">
        <ScoreBadge label="" value={getValue<string | null>()} />
      </div>
    ),
    sortingFn: (a, b, id) =>
      numericValue(a.getValue(id)) - numericValue(b.getValue(id)),
  },
  {
    accessorKey: "total_score",
    header: makeSortableHeader("Total"),
    cell: ({ getValue }) => (
      <div className="text-right tabular-nums">
        <ScoreBadge label="" value={getValue<string | null>()} />
      </div>
    ),
    sortingFn: (a, b, id) =>
      numericValue(a.getValue(id)) - numericValue(b.getValue(id)),
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
            <Badge
              key={f}
              variant={f === "insider_suspect" ? "destructive" : "outline"}
              className="px-1.5 py-0 text-xs"
            >
              {f}
            </Badge>
          ))}
        </div>
      );
    },
    // filterValue: "yes" => has flags, "no" => no flags, "" => all.
    filterFn: (row, _id, filterValue: string) => {
      if (!filterValue) return true;
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
    enableSorting: false,
  },
  {
    accessorKey: "last_active_at",
    header: makeSortableHeader("Last Active"),
    cell: ({ getValue }) => {
      const v = getValue<Date | null>();
      if (!v) return <span className="text-xs text-muted-foreground">—</span>;
      return (
        <span className="whitespace-nowrap text-xs text-muted-foreground">
          {new Date(v).toLocaleDateString("en-CA")}
        </span>
      );
    },
    sortingFn: (a, b) => {
      const av = a.getValue<Date | null>("last_active_at");
      const bv = b.getValue<Date | null>("last_active_at");
      return (
        (av ? new Date(av).getTime() : 0) - (bv ? new Date(bv).getTime() : 0)
      );
    },
  },
  {
    id: "actions",
    header: () => <span className="sr-only">Actions</span>,
    cell: ({ row }) => (
      <div className="flex justify-end">
        <RowActions row={row.original} />
      </div>
    ),
    enableSorting: false,
    enableHiding: false,
  },
];

// Right-aligned numeric columns (header alignment to match the cells).
const NUMERIC_COLUMNS = new Set([
  "profit_score",
  "replicability_score",
  "total_score",
]);

// ---------------------------------------------------------------------------
// Faceted filter dropdown (single-select checkbox list, shadcn pattern)
// ---------------------------------------------------------------------------
interface FacetedFilterProps {
  title: string;
  value: string;
  options: readonly { label: string; value: string }[];
  onChange: (next: string) => void;
}

function FacetedFilter({
  title,
  value,
  options,
  onChange,
}: FacetedFilterProps) {
  const active = options.find((o) => o.value === value);
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="outline" size="sm" className="h-8 text-xs">
          <ListFilter className="mr-1 h-3.5 w-3.5" aria-hidden="true" />
          {title}
          {active && (
            <Badge
              variant="secondary"
              className="ml-2 rounded-sm px-1 py-0 text-[10px] font-normal"
            >
              {active.label}
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
          All
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
// Component
// ---------------------------------------------------------------------------
interface DiscoveryTableProps {
  data: CandidateLeaderRow[];
}

export function DiscoveryTable({ data }: DiscoveryTableProps) {
  const router = useRouter();
  const [sorting, setSorting] = React.useState<SortingState>([
    { id: "total_score", desc: true },
  ]);
  const [columnFilters, setColumnFilters] = React.useState<ColumnFiltersState>(
    []
  );
  const [columnVisibility, setColumnVisibility] =
    React.useState<VisibilityState>({});
  const [globalFilter, setGlobalFilter] = React.useState("");

  const table = useReactTable({
    data,
    columns,
    state: { sorting, columnFilters, columnVisibility, globalFilter },
    onSortingChange: setSorting,
    onColumnFiltersChange: setColumnFilters,
    onColumnVisibilityChange: setColumnVisibility,
    onGlobalFilterChange: setGlobalFilter,
    globalFilterFn: (row, _columnId, filterValue: string) => {
      const q = String(filterValue).trim().toLowerCase();
      if (!q) return true;
      const wallet = String(
        row.getValue("wallet_address") ?? ""
      ).toLowerCase();
      const name = String(row.getValue("display_name") ?? "").toLowerCase();
      return wallet.includes(q) || name.includes(q);
    },
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getFilteredRowModel: getFilteredRowModel(),
    getPaginationRowModel: getPaginationRowModel(),
    initialState: { pagination: { pageSize: 25 } },
  });

  const statusFilter =
    (table.getColumn("user_status")?.getFilterValue() as string) ?? "";
  const strategyFilter =
    (table.getColumn("strategy_type")?.getFilterValue() as string) ?? "";
  const riskFilter =
    (table.getColumn("risk_flags")?.getFilterValue() as string) ?? "";

  const isFiltered =
    globalFilter !== "" ||
    statusFilter !== "" ||
    strategyFilter !== "" ||
    riskFilter !== "";

  function resetFilters() {
    setGlobalFilter("");
    table.getColumn("user_status")?.setFilterValue("");
    table.getColumn("strategy_type")?.setFilterValue("");
    table.getColumn("risk_flags")?.setFilterValue("");
  }

  const pagination = table.getState().pagination;
  const filteredCount = table.getFilteredRowModel().rows.length;
  const pageRows = table.getRowModel().rows;
  const pageCount = table.getPageCount();
  const firstRow =
    filteredCount === 0 ? 0 : pagination.pageIndex * pagination.pageSize + 1;
  const lastRow = Math.min(
    (pagination.pageIndex + 1) * pagination.pageSize,
    filteredCount
  );
  const visibleColCount = table.getVisibleLeafColumns().length;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-xl">
          Candidate Leaders
          <Badge variant="secondary" className="rounded-full">
            {data.length}
          </Badge>
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        {/* Toolbar */}
        <div className="flex flex-wrap items-center gap-2">
          <div className="relative">
            <Search
              className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground"
              aria-hidden="true"
            />
            <Input
              placeholder="Search wallet or name…"
              aria-label="Search by wallet or display name"
              value={globalFilter}
              onChange={(e) => setGlobalFilter(e.target.value)}
              className="h-8 w-[220px] pl-8"
            />
          </div>

          <FacetedFilter
            title="Strategy"
            value={strategyFilter}
            options={STRATEGY_OPTIONS}
            onChange={(v) =>
              table.getColumn("strategy_type")?.setFilterValue(v)
            }
          />
          <FacetedFilter
            title="Status"
            value={statusFilter}
            options={STATUS_OPTIONS}
            onChange={(v) =>
              table.getColumn("user_status")?.setFilterValue(v)
            }
          />
          <FacetedFilter
            title="Risk Flag"
            value={riskFilter}
            options={[
              { label: "Has risk flag", value: "yes" },
              { label: "No risk flag", value: "no" },
            ]}
            onChange={(v) => table.getColumn("risk_flags")?.setFilterValue(v)}
          />

          {isFiltered && (
            <Button
              variant="ghost"
              size="sm"
              className="h-8 px-2 text-xs"
              onClick={resetFilters}
            >
              Reset
              <X className="ml-1 h-3.5 w-3.5" aria-hidden="true" />
            </Button>
          )}

          {/* Column visibility (canonical shadcn data-table pattern) */}
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button
                variant="outline"
                size="sm"
                className="ml-auto h-8 text-xs"
              >
                <SlidersHorizontal
                  className="mr-1 h-3.5 w-3.5"
                  aria-hidden="true"
                />
                Columns
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="w-40">
              <DropdownMenuLabel className="text-xs">
                Toggle columns
              </DropdownMenuLabel>
              <DropdownMenuSeparator />
              {table
                .getAllColumns()
                .filter((col) => col.getCanHide())
                .map((col) => (
                  <DropdownMenuCheckboxItem
                    key={col.id}
                    className="text-xs"
                    checked={col.getIsVisible()}
                    onCheckedChange={(value) =>
                      col.toggleVisibility(!!value)
                    }
                  >
                    {COLUMN_LABELS[col.id] ?? col.id.replace(/_/g, " ")}
                  </DropdownMenuCheckboxItem>
                ))}
            </DropdownMenuContent>
          </DropdownMenu>
        </div>

        {/* Table (horizontal scroll wrapper for small screens via shadcn Table) */}
        <div className="rounded-md border">
          <Table>
            <TableHeader>
              {table.getHeaderGroups().map((hg) => (
                <TableRow key={hg.id}>
                  {hg.headers.map((header) => {
                    const sorted = header.column.getIsSorted();
                    const ariaSort: React.AriaAttributes["aria-sort"] =
                      sorted === "asc"
                        ? "ascending"
                        : sorted === "desc"
                          ? "descending"
                          : header.column.getCanSort()
                            ? "none"
                            : undefined;
                    return (
                      <TableHead
                        key={header.id}
                        aria-sort={ariaSort}
                        className={cn(
                          NUMERIC_COLUMNS.has(header.column.id) &&
                            "text-right"
                        )}
                      >
                        {header.isPlaceholder
                          ? null
                          : flexRender(
                              header.column.columnDef.header,
                              header.getContext()
                            )}
                      </TableHead>
                    );
                  })}
                </TableRow>
              ))}
            </TableHeader>
            <TableBody>
              {pageRows.length ? (
                pageRows.map((row) => {
                  const wallet = row.original.wallet_address;
                  return (
                    <TableRow
                      key={row.id}
                      tabIndex={0}
                      aria-label={`View leader ${wallet}`}
                      className="cursor-pointer outline-none hover:bg-muted/50 focus-visible:bg-muted/50 focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset"
                      onClick={() => router.push(`/discovery/${wallet}`)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" || e.key === " ") {
                          e.preventDefault();
                          router.push(`/discovery/${wallet}`);
                        }
                      }}
                    >
                      {row.getVisibleCells().map((cell) => (
                        <TableCell
                          key={cell.id}
                          className={cn(
                            "py-2",
                            NUMERIC_COLUMNS.has(cell.column.id) &&
                              "text-right tabular-nums"
                          )}
                        >
                          {flexRender(
                            cell.column.columnDef.cell,
                            cell.getContext()
                          )}
                        </TableCell>
                      ))}
                    </TableRow>
                  );
                })
              ) : (
                <TableRow>
                  <TableCell
                    colSpan={visibleColCount}
                    className="h-32 text-center"
                  >
                    <div className="flex flex-col items-center justify-center gap-1 text-sm">
                      <span className="font-medium">
                        No candidate leaders found
                      </span>
                      <span className="text-xs text-muted-foreground">
                        {isFiltered
                          ? "Try clearing filters or adjusting your search."
                          : "Run discovery to populate the candidate pool."}
                      </span>
                      {isFiltered && (
                        <Button
                          variant="outline"
                          size="sm"
                          className="mt-2 h-7 text-xs"
                          onClick={resetFilters}
                        >
                          Clear filters
                        </Button>
                      )}
                    </div>
                  </TableCell>
                </TableRow>
              )}
            </TableBody>
          </Table>
        </div>

        {/* Pagination footer */}
        <div className="flex flex-wrap items-center justify-between gap-3 text-sm text-muted-foreground">
          <span>
            {filteredCount === 0
              ? "0 of 0"
              : `${firstRow}–${lastRow} of ${filteredCount}`}
          </span>

          <div className="flex flex-wrap items-center gap-4">
            <div className="flex items-center gap-2">
              <span className="text-xs">Rows per page</span>
              <Select
                value={String(pagination.pageSize)}
                onValueChange={(v) => table.setPageSize(Number(v))}
              >
                <SelectTrigger
                  className="h-8 w-[4.5rem]"
                  aria-label="Rows per page"
                >
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {PAGE_SIZE_OPTIONS.map((n) => (
                    <SelectItem key={n} value={String(n)}>
                      {n}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            <span className="min-w-[5.5rem] text-center text-xs">
              Page {pagination.pageIndex + 1} of {Math.max(pageCount, 1)}
            </span>

            <div className="flex items-center gap-1">
              <Button
                variant="outline"
                size="icon"
                className="h-7 w-7"
                aria-label="Previous page"
                onClick={() => table.previousPage()}
                disabled={!table.getCanPreviousPage()}
              >
                <ChevronLeft className="h-4 w-4" aria-hidden="true" />
              </Button>
              <Button
                variant="outline"
                size="icon"
                className="h-7 w-7"
                aria-label="Next page"
                onClick={() => table.nextPage()}
                disabled={!table.getCanNextPage()}
              >
                <ChevronRight className="h-4 w-4" aria-hidden="true" />
              </Button>
            </div>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
