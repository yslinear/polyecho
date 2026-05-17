/**
 * /leaders — Followed leaders and copy-trade status (read-only).
 *
 * Server Component: reads follow_relations joined to accounts + copy
 * templates so each row shows which account copies which leader under
 * which template. Pause/resume is a deliberate write that lands later;
 * for now the empty state names the real next action (CLI follow).
 */

import { asc, eq } from "drizzle-orm";

import { db } from "@/lib/db";
import { followRelations, accounts, copyTemplates } from "@/lib/db/schema";
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

export const dynamic = "force-dynamic";

export type LeaderRow = {
  id: string;
  leader_address: string;
  account_name: string | null;
  template_name: string | null;
  status: string;
  created_at: Date;
  paused_at: Date | null;
};

export default async function LeadersPage() {
  const rows: LeaderRow[] = await db
    .select({
      id: followRelations.id,
      leader_address: followRelations.leader_address,
      account_name: accounts.name,
      template_name: copyTemplates.name,
      status: followRelations.status,
      created_at: followRelations.created_at,
      paused_at: followRelations.paused_at,
    })
    .from(followRelations)
    .leftJoin(accounts, eq(followRelations.account_id, accounts.id))
    .leftJoin(copyTemplates, eq(followRelations.template_id, copyTemplates.id))
    .orderBy(asc(followRelations.created_at));

  const active = rows.filter((r) => r.status === "active").length;

  return (
    <div className="space-y-8">
      <header className="space-y-1.5">
        <h1 className="text-2xl font-bold tracking-tight">Leaders</h1>
        <p className="text-sm text-muted-foreground">
          {rows.length === 0
            ? "Leaders you copy and the template each follow uses."
            : `${rows.length} relationship${rows.length === 1 ? "" : "s"} · ${active} active.`}
        </p>
      </header>

      {rows.length === 0 ? (
        <div className="rounded-lg border border-dashed py-16 text-center">
          <p className="text-sm text-muted-foreground">
            Not following anyone yet. Pick a candidate on{" "}
            <span className="font-medium text-foreground">Discovery</span>, then
            start copying from the CLI with{" "}
            <code className="rounded bg-muted px-1 py-0.5 font-mono text-xs">
              polyecho follow &lt;leader&gt;
            </code>
            .
          </p>
        </div>
      ) : (
        <div className="overflow-hidden rounded-lg border">
          <Table>
            <TableHeader className="sticky top-0 z-10 bg-muted/60 backdrop-blur supports-[backdrop-filter]:bg-muted/60">
              <TableRow className="hover:bg-transparent">
                <TableHead>Leader</TableHead>
                <TableHead>Account</TableHead>
                <TableHead>Template</TableHead>
                <TableHead>Status</TableHead>
                <TableHead className="text-right">Followed</TableHead>
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
                      <span className="text-muted-foreground">—</span>
                    )}
                  </TableCell>
                  <TableCell className="text-sm">
                    {r.template_name ?? (
                      <span className="text-muted-foreground">—</span>
                    )}
                  </TableCell>
                  <TableCell>
                    <Badge
                      variant={
                        r.status === "active" ? "default" : "secondary"
                      }
                      className="capitalize text-xs"
                    >
                      {r.status}
                    </Badge>
                  </TableCell>
                  <TableCell className="whitespace-nowrap text-right text-xs text-muted-foreground tabular-nums">
                    {formatDateTime(r.created_at)}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}
    </div>
  );
}
