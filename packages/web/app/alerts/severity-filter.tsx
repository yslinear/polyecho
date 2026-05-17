"use client";

/**
 * SeverityFilter — calm client-side filter for the events list.
 *
 * No URL round-trip / no DB re-query: the Server Component hands the recent
 * events down once and we filter in place (the list is bounded). A plain
 * segmented control, not a modal, matching the instrument register.
 */

import * as React from "react";

import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Button } from "@/components/ui/button";
import { formatDateTime } from "@/lib/format";
import {
  eventTypeLabel,
  severityMeta,
  summarisePayload,
  type Severity,
} from "@/lib/alerts";

export interface AlertEventRow {
  id: number;
  event_type: string;
  severity: string;
  payload: unknown;
  pushed_at: Date | string | null;
  created_at: Date | string;
}

type Filter = "all" | Severity;

const FILTERS: { value: Filter; label: string }[] = [
  { value: "all", label: "全部" },
  { value: "critical", label: "CRITICAL" },
  { value: "warning", label: "WARNING" },
  { value: "info", label: "INFO" },
];

export function AlertsList({ rows }: { rows: AlertEventRow[] }) {
  const [filter, setFilter] = React.useState<Filter>("all");

  const visible = React.useMemo(
    () =>
      filter === "all"
        ? rows
        : rows.filter((r) => r.severity === filter),
    [rows, filter]
  );

  return (
    <div className="space-y-4">
      <div
        className="flex flex-wrap items-center gap-2"
        role="group"
        aria-label="依嚴重度篩選警示"
      >
        {FILTERS.map((f) => (
          <Button
            key={f.value}
            type="button"
            size="sm"
            variant={filter === f.value ? "default" : "outline"}
            className="h-8 text-xs"
            aria-pressed={filter === f.value}
            onClick={() => setFilter(f.value)}
          >
            {f.label}
          </Button>
        ))}
      </div>

      {visible.length === 0 ? (
        <div className="rounded-lg border border-dashed py-16 text-center">
          <p className="text-sm text-muted-foreground">
            {rows.length === 0
              ? "目前沒有任何警示。系統發生重要事件時會出現在這裡。"
              : "此嚴重度沒有符合的警示。"}
          </p>
        </div>
      ) : (
        <div className="overflow-hidden rounded-lg border">
          <Table>
            <TableHeader className="sticky top-0 z-10 bg-muted/60 backdrop-blur supports-[backdrop-filter]:bg-muted/60">
              <TableRow className="hover:bg-transparent">
                <TableHead className="w-28">嚴重度</TableHead>
                <TableHead>事件</TableHead>
                <TableHead>摘要</TableHead>
                <TableHead className="w-24 text-right">推播</TableHead>
                <TableHead className="w-44 text-right">時間</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {visible.map((r) => {
                const meta = severityMeta(r.severity);
                const summary = summarisePayload(r.payload);
                return (
                  <TableRow key={r.id} className="hover:bg-muted/50">
                    <TableCell>
                      <Badge variant={meta.variant} className="text-xs">
                        {meta.label}
                      </Badge>
                    </TableCell>
                    <TableCell className="text-sm font-medium">
                      {eventTypeLabel(r.event_type)}
                    </TableCell>
                    <TableCell className="max-w-prose text-sm text-muted-foreground">
                      {summary || "—"}
                    </TableCell>
                    <TableCell className="text-right text-xs">
                      {r.pushed_at ? (
                        <span className="text-gain">已送達</span>
                      ) : (
                        <span className="text-muted-foreground">未推播</span>
                      )}
                    </TableCell>
                    <TableCell className="whitespace-nowrap text-right text-xs tabular-nums text-muted-foreground">
                      {formatDateTime(r.created_at)}
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
