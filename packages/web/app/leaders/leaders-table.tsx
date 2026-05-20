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
import { Pause } from "lucide-react";

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
import { setFollowStatus } from "./actions";

export interface LeaderRow {
  id: string;
  leader_address: string;
  account_name: string | null;
  template_name: string | null;
  status: string;
  created_at: Date | string;
  last_seen_ts: Date | string | null;
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

export function LeadersTable({ rows }: { rows: LeaderRow[] }) {
  return (
    <div className="overflow-x-auto rounded-lg border">
      <Table className="min-w-[44rem]">
        <TableHeader className="sticky top-0 z-10 bg-muted">
          <TableRow className="hover:bg-transparent">
            <TableHead>{t("tables.leaders.col.leader")}</TableHead>
            <TableHead>{t("tables.leaders.col.account")}</TableHead>
            <TableHead>{t("tables.leaders.col.template")}</TableHead>
            <TableHead>{t("tables.leaders.col.status")}</TableHead>
            <TableHead className="text-right">
              {t("tables.leaders.col.lastSeen")}
            </TableHead>
            <TableHead className="text-right">
              {t("tables.leaders.col.followed")}
            </TableHead>
            <TableHead className="text-right">
              {t("tables.leaders.col.action")}
            </TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((r) => (
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
              <TableCell className="text-right">
                <RowAction row={r} />
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}
