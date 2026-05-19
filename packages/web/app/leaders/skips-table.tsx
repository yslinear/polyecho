/**
 * SkipsTable — read-only recent copy_skips view (PolyHermes brief §7:
 * "a queryable skip/error log with a skip-reason taxonomy"). Server-safe
 * (no "use client"): the parent Server Component passes a limited, ordered
 * slice so the operator can see WHY recent copies were skipped. The reason
 * badge uses the frozen SkipReason taxonomy (lib/skip-reasons).
 */

import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { shortAddress, formatDateTime } from "@/lib/format";
import { t } from "@/lib/i18n";
import { skipReasonLabel, skipReasonVariant } from "@/lib/skip-reasons";

export interface SkipRow {
  id: number;
  leader_address: string | null;
  condition_id: string | null;
  skip_reason: string;
  detail: string | null;
  created_at: Date | string | null;
}

export function SkipsTable({
  rows,
  limit,
}: {
  rows: SkipRow[];
  limit: number;
}) {
  return (
    <section className="space-y-3">
      <div className="space-y-1">
        <h2 className="text-base font-semibold">
          {t("tables.leaders.skips.heading")}
        </h2>
        <p className="max-w-prose text-sm text-muted-foreground">
          {rows.length === 0
            ? t("tables.leaders.skips.subtitleEmpty")
            : rows.length === limit
              ? t("tables.leaders.skips.subtitleLast", { limit })
              : t("tables.leaders.skips.subtitleCount", {
                  count: rows.length,
                })}
        </p>
      </div>

      {rows.length === 0 ? (
        <div className="rounded-lg border border-dashed py-12 text-center">
          <p className="text-sm text-muted-foreground">
            {t("tables.leaders.skips.emptyTitle")}
          </p>
        </div>
      ) : (
        <div className="overflow-hidden rounded-lg border">
          <Table>
            <TableHeader className="sticky top-0 z-10 bg-muted">
              <TableRow className="hover:bg-transparent">
                <TableHead>
                  {t("tables.leaders.skips.col.reason")}
                </TableHead>
                <TableHead>
                  {t("tables.leaders.skips.col.leader")}
                </TableHead>
                <TableHead>
                  {t("tables.leaders.skips.col.condition")}
                </TableHead>
                <TableHead>
                  {t("tables.leaders.skips.col.detail")}
                </TableHead>
                <TableHead className="text-right">
                  {t("tables.leaders.skips.col.when")}
                </TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((r) => (
                <TableRow key={r.id} className="hover:bg-muted/50">
                  <TableCell>
                    <Badge
                      variant={skipReasonVariant(r.skip_reason)}
                      className="text-xs"
                    >
                      {skipReasonLabel(r.skip_reason)}
                    </Badge>
                  </TableCell>
                  <TableCell
                    className="font-mono text-xs"
                    title={r.leader_address ?? undefined}
                  >
                    {r.leader_address
                      ? shortAddress(r.leader_address)
                      : t("tables.common.notSet")}
                  </TableCell>
                  <TableCell
                    className="max-w-[200px] truncate font-mono text-xs"
                    title={r.condition_id ?? undefined}
                  >
                    {r.condition_id ?? t("tables.common.notSet")}
                  </TableCell>
                  <TableCell
                    className="max-w-[280px] truncate text-sm text-muted-foreground"
                    title={r.detail ?? undefined}
                  >
                    {r.detail ?? t("tables.common.notSet")}
                  </TableCell>
                  <TableCell className="whitespace-nowrap text-right text-xs tabular-nums text-muted-foreground">
                    {formatDateTime(r.created_at)}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}
    </section>
  );
}
