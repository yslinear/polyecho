/**
 * /leaders — Followed leaders, copy-trade status, and the skip log.
 *
 * Server Component. Reads:
 *  - follow_relations ⋈ accounts ⋈ copy_templates (which account copies
 *    which leader under which template, plus the reconcile cursor).
 *  - settings row keyed `live_trading_ack` (CLI-written audit record) for
 *    the READ-ONLY live-trading status pill.
 *  - recent copy_skips for the read-only skip log (PolyHermes brief §7).
 *
 * Writes: NONE here. The per-row Pause/Resume is a PRD §5.7 SAFE write in
 * the LeadersTable client island via the `setFollowStatus` Server Action.
 * Live-trading enable/disable is HIGH-DANGER and CLI-only — the dashboard
 * deliberately shows only a read-only indicator (PRD §5.7 操作分級).
 */

import { asc, desc, eq } from "drizzle-orm";

import { db } from "@/lib/db";
import {
  followRelations,
  accounts,
  copyTemplates,
  settings,
  copySkips,
} from "@/lib/db/schema";
import { LiveTradingStatus } from "@/components/live-trading-status";
import { LeadersTable, type LeaderRow } from "./leaders-table";
import { SkipsTable, type SkipRow } from "./skips-table";

export const dynamic = "force-dynamic";

const LIVE_ACK_KEY = "live_trading_ack";
const SKIPS_LIMIT = 50;

export default async function LeadersPage() {
  const rows: LeaderRow[] = await db
    .select({
      id: followRelations.id,
      leader_address: followRelations.leader_address,
      account_name: accounts.name,
      template_name: copyTemplates.name,
      status: followRelations.status,
      created_at: followRelations.created_at,
      last_seen_ts: followRelations.last_seen_ts,
    })
    .from(followRelations)
    .leftJoin(accounts, eq(followRelations.account_id, accounts.id))
    .leftJoin(copyTemplates, eq(followRelations.template_id, copyTemplates.id))
    .orderBy(asc(followRelations.created_at));

  const [ackRow] = await db
    .select({ key: settings.key })
    .from(settings)
    .where(eq(settings.key, LIVE_ACK_KEY))
    .limit(1);

  const skips: SkipRow[] = await db
    .select({
      id: copySkips.id,
      leader_address: copySkips.leader_address,
      condition_id: copySkips.condition_id,
      skip_reason: copySkips.skip_reason,
      detail: copySkips.detail,
      created_at: copySkips.created_at,
    })
    .from(copySkips)
    .orderBy(desc(copySkips.created_at))
    .limit(SKIPS_LIMIT);

  const acked = Boolean(ackRow);
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

      <section className="rounded-lg border p-4">
        <LiveTradingStatus acked={acked} variant="verbose" />
      </section>

      {rows.length === 0 ? (
        <div className="rounded-lg border border-dashed py-16 text-center">
          <p className="text-sm text-muted-foreground">
            Not following anyone yet. Promote a candidate from{" "}
            <span className="font-medium text-foreground">Discovery</span>,
            then start copying from the CLI with{" "}
            <code className="rounded bg-muted px-1 py-0.5 font-mono text-xs">
              polyecho discovery promote &lt;wallet&gt;
            </code>
            .
          </p>
        </div>
      ) : (
        <LeadersTable rows={rows} />
      )}

      <SkipsTable rows={skips} limit={SKIPS_LIMIT} />
    </div>
  );
}
