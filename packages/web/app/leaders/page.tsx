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
import { t } from "@/lib/i18n";
import { lintTemplateConfig } from "@/lib/template-linter";
import { LeadersTable, type LeaderRow } from "./leaders-table";
import { SkipsTable, type SkipRow } from "./skips-table";

export const dynamic = "force-dynamic";

const LIVE_ACK_KEY = "live_trading_ack";
const SKIPS_LIMIT = 50;

export default async function LeadersPage() {
  // The template's risk-cap columns are joined in so the B3 pre-flight
  // linter (lib/template-linter) can flag a dangerously-configured template
  // BEFORE the operator resumes a paused live follow. The 16-step gate still
  // enforces caps at trade time; this is a calm advisory, not a blocker.
  const joined = await db
    .select({
      id: followRelations.id,
      leader_address: followRelations.leader_address,
      account_name: accounts.name,
      template_name: copyTemplates.name,
      status: followRelations.status,
      created_at: followRelations.created_at,
      last_seen_ts: followRelations.last_seen_ts,
      max_position_usd: copyTemplates.max_position_usd,
      daily_loss_limit_usd: copyTemplates.daily_loss_limit_usd,
      max_concurrent_positions: copyTemplates.max_concurrent_positions,
    })
    .from(followRelations)
    .leftJoin(accounts, eq(followRelations.account_id, accounts.id))
    .leftJoin(copyTemplates, eq(followRelations.template_id, copyTemplates.id))
    .orderBy(asc(followRelations.created_at));

  const rows: LeaderRow[] = joined.map((r) => ({
    id: r.id,
    leader_address: r.leader_address,
    account_name: r.account_name,
    template_name: r.template_name,
    status: r.status,
    created_at: r.created_at,
    last_seen_ts: r.last_seen_ts,
    // Lint server-side: the warnings are small + serialise cleanly to the
    // client island, which renders them as the calm advisory.
    templateWarnings: lintTemplateConfig({
      max_position_usd: r.max_position_usd,
      daily_loss_limit_usd: r.daily_loss_limit_usd,
      max_concurrent_positions: r.max_concurrent_positions,
    }),
  }));

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
        <h1 className="text-2xl font-bold tracking-tight">
          {t("tables.leaders.title")}
        </h1>
        <p className="text-sm text-muted-foreground">
          {rows.length === 0
            ? t("tables.leaders.subtitleEmpty")
            : t("tables.leaders.subtitleCount", {
                count: rows.length,
                active,
              })}
        </p>
      </header>

      <LiveTradingStatus acked={acked} variant="verbose" />

      {rows.length === 0 ? (
        <div className="rounded-lg border border-dashed py-16 text-center">
          <p className="text-sm text-muted-foreground">
            {t("tables.leaders.emptyPrefix")}{" "}
            <span className="font-medium text-foreground">
              {t("tables.leaders.emptyDiscovery")}
            </span>{" "}
            {t("tables.leaders.emptyMiddle")}{" "}
            <code className="rounded bg-muted px-1 py-0.5 font-mono text-xs">
              {/* Literal CLI command — proper noun per common.ts
                  PROPER-NOUN POLICY, intentionally not translated. */}
              {"polyecho discovery promote <wallet>"}
            </code>{" "}
            {t("tables.leaders.emptySuffix")}
          </p>
        </div>
      ) : (
        <LeadersTable rows={rows} />
      )}

      <SkipsTable rows={skips} limit={SKIPS_LIMIT} />
    </div>
  );
}
