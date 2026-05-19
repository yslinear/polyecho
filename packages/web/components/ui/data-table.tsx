"use client";

import * as React from "react";
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
import { ArrowUpDown, ChevronDown, ChevronLeft, ChevronRight } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
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
import { t } from "@/lib/i18n";
import { cn } from "@/lib/utils";

// ---------------------------------------------------------------------------
// Re-export column def helper so consumers don't need direct tanstack import
// ---------------------------------------------------------------------------
export type { ColumnDef };
export { ArrowUpDown };

// ---------------------------------------------------------------------------
// DataTable component
// ---------------------------------------------------------------------------
interface DataTableProps<TData, TValue> {
  columns: ColumnDef<TData, TValue>[];
  data: TData[];
  /** Column id for free-text search (searches that column's value) */
  searchKey?: string;
  searchPlaceholder?: string;
  /** Faceted filter configs */
  facetedFilters?: FacetedFilter[];
  pageSize?: number;
  className?: string;
  /**
   * Honest empty-state node shown when there are zero rows. Pages should pass
   * a calm one-sentence message plus the real next action (DESIGN.md "Tables:
   * real empty state"; PRODUCT.md honest-empty). Defaults to a generic line.
   */
  emptyState?: React.ReactNode;
}

interface FacetedFilter {
  columnId: string;
  title: string;
  options: { label: string; value: string }[];
}

export function DataTable<TData, TValue>({
  columns,
  data,
  searchKey,
  searchPlaceholder = t("common.dataTable.search"),
  facetedFilters = [],
  pageSize = 25,
  className,
  emptyState = t("common.dataTable.empty"),
}: DataTableProps<TData, TValue>) {
  const [sorting, setSorting] = React.useState<SortingState>([]);
  const [columnFilters, setColumnFilters] = React.useState<ColumnFiltersState>([]);
  const [columnVisibility, setColumnVisibility] = React.useState<VisibilityState>({});

  const table = useReactTable({
    data,
    columns,
    onSortingChange: setSorting,
    onColumnFiltersChange: setColumnFilters,
    onColumnVisibilityChange: setColumnVisibility,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getFilteredRowModel: getFilteredRowModel(),
    getPaginationRowModel: getPaginationRowModel(),
    initialState: { pagination: { pageSize } },
    state: {
      sorting,
      columnFilters,
      columnVisibility,
    },
  });

  return (
    <div className={cn("space-y-3", className)}>
      {/* Toolbar */}
      <div className="flex flex-wrap items-center gap-2">
        {searchKey && (
          <Input
            placeholder={searchPlaceholder}
            value={(table.getColumn(searchKey)?.getFilterValue() as string) ?? ""}
            onChange={(e) =>
              table.getColumn(searchKey)?.setFilterValue(e.target.value)
            }
            className="max-w-xs h-8"
          />
        )}

        {/* Faceted filters */}
        {facetedFilters.map((ff) => {
          const col = table.getColumn(ff.columnId);
          if (!col) return null;
          const currentValue = (col.getFilterValue() as string) ?? "";

          return (
            <DropdownMenu key={ff.columnId}>
              <DropdownMenuTrigger asChild>
                <Button variant="outline" size="sm" className="h-8 text-xs">
                  {ff.title}
                  {currentValue && (
                    <span className="ml-1 rounded-sm bg-primary/10 px-1 text-primary">
                      {currentValue}
                    </span>
                  )}
                  <ChevronDown className="ml-1 h-3 w-3 opacity-50" />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="start" className="w-40">
                <DropdownMenuCheckboxItem
                  checked={!currentValue}
                  onCheckedChange={() => col.setFilterValue("")}
                >
                  {t("common.dataTable.all")}
                </DropdownMenuCheckboxItem>
                {ff.options.map((opt) => (
                  <DropdownMenuCheckboxItem
                    key={opt.value}
                    checked={currentValue === opt.value}
                    onCheckedChange={() =>
                      col.setFilterValue(
                        currentValue === opt.value ? "" : opt.value
                      )
                    }
                  >
                    {opt.label}
                  </DropdownMenuCheckboxItem>
                ))}
              </DropdownMenuContent>
            </DropdownMenu>
          );
        })}

        {/* Column visibility */}
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button variant="outline" size="sm" className="ml-auto h-8 text-xs">
              {t("common.dataTable.columns")}{" "}
              <ChevronDown className="ml-1 h-3 w-3 opacity-50" />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="w-36">
            {table
              .getAllColumns()
              .filter((col) => col.getCanHide())
              .map((col) => (
                <DropdownMenuCheckboxItem
                  key={col.id}
                  className="capitalize text-xs"
                  checked={col.getIsVisible()}
                  onCheckedChange={(value) => col.toggleVisibility(!!value)}
                >
                  {col.id.replace(/_/g, " ")}
                </DropdownMenuCheckboxItem>
              ))}
          </DropdownMenuContent>
        </DropdownMenu>
      </div>

      {/* Table */}
      <div className="rounded-lg border overflow-hidden">
        <Table>
          <TableHeader className="sticky top-0 z-10 bg-muted">
            {table.getHeaderGroups().map((hg) => (
              <TableRow key={hg.id}>
                {hg.headers.map((header) => {
                  const sortDir = header.column.getIsSorted();
                  const ariaSort = !header.column.getCanSort()
                    ? undefined
                    : sortDir === "asc"
                      ? "ascending"
                      : sortDir === "desc"
                        ? "descending"
                        : "none";
                  return (
                    <TableHead key={header.id} aria-sort={ariaSort}>
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
            {table.getRowModel().rows?.length ? (
              table.getRowModel().rows.map((row) => (
                <TableRow
                  key={row.id}
                  data-state={row.getIsSelected() && "selected"}
                >
                  {row.getVisibleCells().map((cell) => (
                    <TableCell key={cell.id}>
                      {flexRender(
                        cell.column.columnDef.cell,
                        cell.getContext()
                      )}
                    </TableCell>
                  ))}
                </TableRow>
              ))
            ) : (
              <TableRow>
                <TableCell
                  colSpan={columns.length}
                  className="h-24 text-center text-muted-foreground text-sm"
                >
                  {emptyState}
                </TableCell>
              </TableRow>
            )}
          </TableBody>
        </Table>
      </div>

      {/* Pagination */}
      <div className="flex items-center justify-between text-sm text-muted-foreground">
        <span>
          {t("common.dataTable.rowCount", {
            count: table.getFilteredRowModel().rows.length,
          })}
        </span>
        <div className="flex items-center gap-1">
          <Button
            variant="outline"
            size="icon"
            className="h-7 w-7"
            onClick={() => table.previousPage()}
            disabled={!table.getCanPreviousPage()}
            aria-label={t("common.dataTable.previousPage")}
          >
            <ChevronLeft className="h-4 w-4" />
          </Button>
          <span className="min-w-[5rem] text-center">
            {t("common.dataTable.pageOf", {
              page: table.getState().pagination.pageIndex + 1,
              total: table.getPageCount(),
            })}
          </span>
          <Button
            variant="outline"
            size="icon"
            className="h-7 w-7"
            onClick={() => table.nextPage()}
            disabled={!table.getCanNextPage()}
            aria-label={t("common.dataTable.nextPage")}
          >
            <ChevronRight className="h-4 w-4" />
          </Button>
        </div>
      </div>
    </div>
  );
}
