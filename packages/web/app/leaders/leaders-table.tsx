"use client";

/**
 * LeadersTable — client island for the followed-leaders relationships.
 *
 * Read-mostly: the only write is the per-row Pause/Resume, a PRD §5.7 SAFE
 * write delegated to the `setFollowStatus` Server Action. After it resolves
 * we toast + router.refresh() so the Server Component re-reads the row
 * (mirrors the discovery-table optimistic-free refresh pattern).
 *
 * There is deliberately NO live-trading control here — that is high-danger
 * and CLI-only (PRD §5.7). The header pill is a read-only indicator.
 */

import * as React from "react";
import { useRouter } from "next/navigation";
import { toast } from "sonner";
import { Pause, ArrowDown, ArrowUp, ChevronsUpDown } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { InlineConfirm } from "@/components/inline-confirm";
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
import {
  hasHighSeverity,
  type TemplateWarning,
} from "@/lib/template-linter";
import { setFollowStatus } from "./actions";

export interface LeaderRow {
  id: string;
  leader_address: string;
  account_name: string | null;
  template_name: string | null;
  status: string;
  created_at: Date | string;
  last_seen_ts: Date | string | null;
  /** B3 — pre-flight TemplateConfig risk-linter findings (lib/template-linter). */
  templateWarnings: TemplateWarning[];
}

/**
 * TemplateRiskAdvisory — the B3 pre-flight advisory (PolyHermes
 * `inspectRiskConfig`). Renders the linter findings as a calm Badge-led
 * notice so the operator sees a dangerously-configured template BEFORE
 * resuming a live follow. DESIGN.md: Badge variants carry the severity (no
 * raw colour-only signal); `outline` for the medium-only case so the row
 * stays quiet, `destructive` only when a money-loss cap is unset.
 */
function TemplateRiskAdvisory({
  warnings,
}: {
  warnings: TemplateWarning[];
}) {
  if (warnings.length === 0) return null;
  const high = hasHighSeverity(warnings);
  return (
    <div className="mt-2 space-y-1.5 rounded-md border border-dashed bg-muted/40 p-2">
      <div className="flex items-center gap-1.5">
        <Badge variant={high ? "destructive" : "outline"} className="text-xs">
          {t("tables.leaders.templateRisk.heading")}
        </Badge>
        <span className="text-xs text-muted-foreground">
          {high
            ? t("tables.leaders.templateRisk.summaryHigh", {
                count: warnings.length,
              })
            : t("tables.leaders.templateRisk.summaryMedium", {
                count: warnings.length,
              })}
        </span>
      </div>
      <ul className="space-y-1">
        {warnings.map((w) => (
          <li key={w.code} className="flex items-start gap-1.5 text-xs">
            <Badge
              variant={w.severity === "high" ? "destructive" : "secondary"}
              className="shrink-0 text-[0.65rem]"
            >
              {w.severity === "high"
                ? t("tables.leaders.templateRisk.severityHigh")
                : t("tables.leaders.templateRisk.severityMedium")}
            </Badge>
            <span className="text-muted-foreground">
              {t(w.messageKey)}{" "}
              <span className="text-foreground">
                {t("tables.leaders.templateRisk.suggestedLabel", {
                  value: w.suggested,
                })}
              </span>
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function RowAction({ row }: { row: LeaderRow }) {
  const router = useRouter();
  const [pending, setPending] = React.useState(false);
  const isActive = row.status === "active";

  // P7d / R24 asymmetry:
  //  - PAUSE (active → paused): safe-direction → single click.
  //  - RESUME (paused → active): risk-increasing → inline-confirm primitive.
  if (!isActive) {
    return (
      <div className="flex justify-end">
        <InlineConfirm
          title={t("tables.leaders.resume")}
          description={t("tables.leaders.resumeConfirmDescription", {
            address: row.leader_address,
          })}
          requirePassphrase={false}
          triggerVariant="default"
          triggerClassName="h-8 gap-1.5 text-xs"
          successToast={t("tables.leaders.resumedToast")}
          action={async ({ confirmText }) => {
            try {
              await setFollowStatus(row.id, "active", { confirmText });
              return { ok: true, message: "OK" };
            } catch (err) {
              return {
                ok: false,
                message:
                  err instanceof Error
                    ? err.message
                    : t("tables.leaders.updateError"),
              };
            }
          }}
        />
      </div>
    );
  }

  async function onPause() {
    setPending(true);
    try {
      await setFollowStatus(row.id, "paused");
      toast.success(t("tables.leaders.pausedToast"));
      router.refresh();
    } catch {
      toast.error(t("tables.leaders.updateError"));
    } finally {
      setPending(false);
    }
  }

  return (
    <Button
      variant="outline"
      size="sm"
      className="h-8 text-xs"
      disabled={pending}
      aria-label={t("tables.leaders.pauseAria", {
        address: row.leader_address,
      })}
      onClick={onPause}
    >
      <Pause className="mr-1 h-3.5 w-3.5" aria-hidden="true" />
      {pending ? t("tables.leaders.pausing") : t("tables.leaders.pause")}
    </Button>
  );
}

// Sortable columns + their comparable key extractors. leader/account/
// template/action are intentionally not sortable (text relations / action
// cell), mirroring discovery-table's unsortable-column treatment.
type SortKey = "status" | "lastSeen" | "followed";
type SortDir = "asc" | "desc";

function sortValue(row: LeaderRow, key: SortKey): number | string {
  switch (key) {
    case "status":
      return row.status;
    case "lastSeen":
      return row.last_seen_ts ? new Date(row.last_seen_ts).getTime() : 0;
    case "followed":
      return new Date(row.created_at).getTime();
  }
}

// Sortable <th> — a ghost Button + trailing sort icon, with aria-sort set on
// the parent <th> for WCAG (identical idiom to discovery-table).
function SortableHead({
  label,
  sortKey,
  active,
  dir,
  onSort,
  align = "left",
}: {
  label: string;
  sortKey: SortKey;
  active: boolean;
  dir: SortDir;
  onSort: (key: SortKey) => void;
  align?: "left" | "right";
}) {
  const Icon = !active ? ChevronsUpDown : dir === "asc" ? ArrowUp : ArrowDown;
  return (
    <TableHead
      aria-sort={active ? (dir === "asc" ? "ascending" : "descending") : "none"}
      className={align === "right" ? "text-right" : undefined}
    >
      <Button
        variant="ghost"
        size="sm"
        className="-ml-3 h-8 data-[active=true]:text-foreground"
        data-active={active}
        onClick={() => onSort(sortKey)}
      >
        {label}
        <Icon className="ml-1 h-3.5 w-3.5" aria-hidden="true" />
      </Button>
    </TableHead>
  );
}

export function LeadersTable({ rows }: { rows: LeaderRow[] }) {
  const [sortKey, setSortKey] = React.useState<SortKey>("followed");
  const [sortDir, setSortDir] = React.useState<SortDir>("asc");

  function onSort(key: SortKey) {
    if (key === sortKey) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      setSortDir("asc");
    }
  }

  const sortedRows = React.useMemo(() => {
    const copy = [...rows];
    copy.sort((a, b) => {
      const av = sortValue(a, sortKey);
      const bv = sortValue(b, sortKey);
      const cmp =
        typeof av === "string" && typeof bv === "string"
          ? av.localeCompare(bv)
          : Number(av) - Number(bv);
      return sortDir === "asc" ? cmp : -cmp;
    });
    return copy;
  }, [rows, sortKey, sortDir]);

  return (
    <div className="overflow-x-auto rounded-lg border">
      <Table className="min-w-[44rem]">
        <TableHeader className="sticky top-0 z-10 bg-muted">
          <TableRow className="hover:bg-transparent">
            <TableHead>{t("tables.leaders.col.leader")}</TableHead>
            <TableHead>{t("tables.leaders.col.account")}</TableHead>
            <TableHead>{t("tables.leaders.col.template")}</TableHead>
            <SortableHead
              label={t("tables.leaders.col.status")}
              sortKey="status"
              active={sortKey === "status"}
              dir={sortDir}
              onSort={onSort}
            />
            <SortableHead
              label={t("tables.leaders.col.lastSeen")}
              sortKey="lastSeen"
              active={sortKey === "lastSeen"}
              dir={sortDir}
              onSort={onSort}
              align="right"
            />
            <SortableHead
              label={t("tables.leaders.col.followed")}
              sortKey="followed"
              active={sortKey === "followed"}
              dir={sortDir}
              onSort={onSort}
              align="right"
            />
            <TableHead className="text-right">
              {t("tables.leaders.col.action")}
            </TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {sortedRows.map((r) => (
            <TableRow key={r.id} className="hover:bg-muted/50">
              <TableCell
                className="font-mono text-xs"
                title={r.leader_address}
              >
                {shortAddress(r.leader_address)}
              </TableCell>
              <TableCell className="text-sm">
                {r.account_name ?? (
                  <span className="text-muted-foreground">
                    {t("tables.common.notSet")}
                  </span>
                )}
              </TableCell>
              <TableCell className="text-sm">
                {r.template_name ?? (
                  <span className="text-muted-foreground">
                    {t("tables.common.notSet")}
                  </span>
                )}
              </TableCell>
              <TableCell>
                <Badge
                  variant={r.status === "active" ? "default" : "secondary"}
                  className="text-xs capitalize"
                >
                  {r.status}
                </Badge>
              </TableCell>
              <TableCell className="whitespace-nowrap text-right text-xs tabular-nums text-muted-foreground">
                {r.last_seen_ts
                  ? formatDateTime(r.last_seen_ts)
                  : t("tables.common.notYet")}
              </TableCell>
              <TableCell className="whitespace-nowrap text-right text-xs tabular-nums text-muted-foreground">
                {formatDateTime(r.created_at)}
              </TableCell>
              <TableCell className="text-right align-top">
                <RowAction row={r} />
                {/* B3 — pre-flight risk advisory, shown next to Resume so the
                    operator reviews a loose/missing-cap template before
                    enabling a live follow. */}
                <div className="text-left">
                  <TemplateRiskAdvisory warnings={r.templateWarnings} />
                </div>
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}
