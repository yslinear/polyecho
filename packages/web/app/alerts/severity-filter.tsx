"use client";

/**
 * SeverityFilter — calm client-side filter for the events list.
 *
 * No URL round-trip / no DB re-query: the Server Component hands the recent
 * events down once and we filter in place (the list is bounded). A plain
 * segmented control, not a modal, matching the instrument register.
 */

import * as React from "react";
import { Check, Minus } from "lucide-react";

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
import { t } from "@/lib/i18n";
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
  { value: "all", label: t("alertsSettings.alerts.list.filterAll") },
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
        aria-label={t("alertsSettings.alerts.list.filterGroupAria")}
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
              ? t("alertsSettings.alerts.list.emptyAll")
              : t("alertsSettings.alerts.list.emptyFiltered")}
          </p>
        </div>
      ) : (
        <div className="overflow-x-auto rounded-lg border">
          <Table>
            <TableHeader className="sticky top-0 z-10 bg-muted">
              <TableRow className="hover:bg-transparent">
                <TableHead className="w-28">
                  {t("alertsSettings.alerts.list.colSeverity")}
                </TableHead>
                <TableHead>
                  {t("alertsSettings.alerts.list.colEvent")}
                </TableHead>
                <TableHead>
                  {t("alertsSettings.alerts.list.colSummary")}
                </TableHead>
                <TableHead className="w-24 text-right">
                  {t("alertsSettings.alerts.list.colPush")}
                </TableHead>
                <TableHead className="w-44 text-right">
                  {t("alertsSettings.alerts.list.colTime")}
                </TableHead>
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
                      {summary || t("alertsSettings.alerts.list.noSummary")}
                    </TableCell>
                    <TableCell className="text-right text-xs">
                      {r.pushed_at ? (
                        <Badge
                          variant="secondary"
                          className="gap-1 text-xs font-medium"
                          aria-label={t(
                            "alertsSettings.alerts.list.pushStatusAria"
                          )}
                        >
                          <Check
                            className="h-3 w-3 shrink-0"
                            aria-hidden="true"
                          />
                          {t("alertsSettings.alerts.list.delivered")}
                        </Badge>
                      ) : (
                        <Badge
                          variant="outline"
                          className="gap-1 text-xs font-medium text-muted-foreground"
                          aria-label={t(
                            "alertsSettings.alerts.list.pushStatusAria"
                          )}
                        >
                          <Minus
                            className="h-3 w-3 shrink-0"
                            aria-hidden="true"
                          />
                          {t("alertsSettings.alerts.list.notPushed")}
                        </Badge>
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
