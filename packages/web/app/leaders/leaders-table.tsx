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
import { Pause, Play } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { shortAddress, formatDateTime } from "@/lib/format";
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
  const next = isActive ? "paused" : "active";

  async function onToggle() {
    setPending(true);
    try {
      await setFollowStatus(row.id, next);
      toast.success(
        next === "paused"
          ? "Leader paused. trader-core stops copying it."
          : "Leader resumed. Copying continues on the next leader trade."
      );
      router.refresh();
    } catch {
      toast.error("Failed to update follow status");
    } finally {
      setPending(false);
    }
  }

  const Icon = isActive ? Pause : Play;
  return (
    <Button
      variant="outline"
      size="sm"
      className="h-8 text-xs"
      disabled={pending}
      aria-label={`${isActive ? "Pause" : "Resume"} copying ${row.leader_address}`}
      onClick={onToggle}
    >
      <Icon className="mr-1 h-3.5 w-3.5" aria-hidden="true" />
      {isActive ? "Pause" : "Resume"}
    </Button>
  );
}

export function LeadersTable({ rows }: { rows: LeaderRow[] }) {
  return (
    <div className="overflow-hidden rounded-lg border">
      <Table>
        <TableHeader className="sticky top-0 z-10 bg-muted">
          <TableRow className="hover:bg-transparent">
            <TableHead>Leader</TableHead>
            <TableHead>Account</TableHead>
            <TableHead>Template</TableHead>
            <TableHead>Status</TableHead>
            <TableHead className="text-right">Last seen</TableHead>
            <TableHead className="text-right">Followed</TableHead>
            <TableHead className="text-right">Action</TableHead>
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
                  <span className="text-muted-foreground">Not set</span>
                )}
              </TableCell>
              <TableCell className="text-sm">
                {r.template_name ?? (
                  <span className="text-muted-foreground">Not set</span>
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
                {r.last_seen_ts ? formatDateTime(r.last_seen_ts) : "Not yet"}
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
