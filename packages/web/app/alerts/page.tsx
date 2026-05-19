/**
 * /alerts — 警示中心 (PRD §5.8 + §5.7).
 *
 * Server Component. Reads (NO writes here; writes are in actions.ts behind
 * client islands):
 *  - recent `events` newest-first (severity, humanised type, payload summary,
 *    delivered? from pushed_at) -> client severity filter.
 *  - `pending_actions` with status awaiting_confirm -> the dangerous-op panel
 *    with a 5s countdown + passphrase confirm island.
 *  - `vapid_public_key` settings row -> handed to the push opt-in island
 *    (PUBLIC key only; the private key never leaves p5-core).
 *
 * The web NEVER emits events or sends pushes — core/push own that. This page
 * only surfaces them and the opt-in / confirm controls.
 */

import { desc, eq } from "drizzle-orm";

import { db } from "@/lib/db";
import { events, pendingActions } from "@/lib/db/schema";
import { PushSubscribe } from "@/components/push-subscribe";
import { getVapidPublicKey } from "./actions";
import { AlertsList, type AlertEventRow } from "./severity-filter";
import {
  CreateDangerousAction,
  ConfirmPendingRow,
} from "./dangerous-action";
import {
  pendingActionLabel,
  timeRemaining,
} from "@/lib/alerts";
import { formatDateTime } from "@/lib/format";
import { t } from "@/lib/i18n";

export const dynamic = "force-dynamic";

const EVENTS_LIMIT = 100;

interface PendingRow {
  id: string;
  action_type: string;
  params: unknown;
  expires_at: Date | string | null;
  created_at: Date | string;
}

function paramsSummary(params: unknown): string {
  if (!params || typeof params !== "object") return "";
  const parts: string[] = [];
  for (const [k, v] of Object.entries(params as Record<string, unknown>)) {
    if (v == null || typeof v === "object") continue;
    parts.push(`${k}: ${String(v)}`);
  }
  return parts.join(" · ");
}

export default async function AlertsPage() {
  const eventRows = (await db
    .select({
      id: events.id,
      event_type: events.event_type,
      severity: events.severity,
      payload: events.payload,
      pushed_at: events.pushed_at,
      created_at: events.created_at,
    })
    .from(events)
    .orderBy(desc(events.created_at))
    .limit(EVENTS_LIMIT)) as AlertEventRow[];

  const pendingRows = (await db
    .select({
      id: pendingActions.id,
      action_type: pendingActions.action_type,
      params: pendingActions.params,
      expires_at: pendingActions.expires_at,
      created_at: pendingActions.created_at,
    })
    .from(pendingActions)
    .where(eq(pendingActions.status, "awaiting_confirm"))
    .orderBy(desc(pendingActions.created_at))) as PendingRow[];

  const vapidPublicKey = await getVapidPublicKey();

  return (
    <div className="space-y-10">
      <header className="space-y-1.5">
        <h1 className="text-2xl font-bold tracking-tight">
          {t("alertsSettings.alerts.title")}
        </h1>
        <p className="max-w-prose text-sm text-muted-foreground">
          {t("alertsSettings.alerts.intro")}
        </p>
      </header>

      <section className="space-y-3">
        <div className="space-y-1">
          <h2 className="text-base font-semibold">
            {t("alertsSettings.alerts.push.heading")}
          </h2>
          <p className="max-w-prose text-sm text-muted-foreground">
            {t("alertsSettings.alerts.push.help")}
          </p>
        </div>
        <PushSubscribe vapidPublicKey={vapidPublicKey} />
      </section>

      <section className="space-y-3 border-t pt-8">
        <div className="space-y-1">
          <h2 className="text-base font-semibold">
            {t("alertsSettings.alerts.pending.heading")}
          </h2>
          <p className="max-w-prose text-sm text-muted-foreground">
            {t("alertsSettings.alerts.pending.help")}
          </p>
        </div>

        <CreateDangerousAction />

        {pendingRows.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            {t("alertsSettings.alerts.pending.empty")}
          </p>
        ) : (
          <ul className="space-y-3">
            {pendingRows.map((p) => (
              <li
                key={p.id}
                className="space-y-3 rounded-lg border p-4"
              >
                <div className="flex flex-wrap items-baseline justify-between gap-2">
                  <span className="text-sm font-semibold">
                    {pendingActionLabel(p.action_type)}
                  </span>
                  <span className="text-xs tabular-nums text-muted-foreground">
                    {t("alertsSettings.alerts.pending.meta", {
                      created: formatDateTime(p.created_at),
                      remaining: timeRemaining(p.expires_at),
                    })}
                  </span>
                </div>
                <p className="font-mono text-xs text-muted-foreground">
                  {paramsSummary(p.params) ||
                    t("alertsSettings.alerts.pending.noParams")}
                </p>
                <ConfirmPendingRow
                  id={p.id}
                  expiresAtIso={
                    p.expires_at
                      ? new Date(p.expires_at).toISOString()
                      : null
                  }
                />
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="space-y-4 border-t pt-8">
        <div className="space-y-1">
          <h2 className="text-base font-semibold">
            {t("alertsSettings.alerts.recent.heading")}
          </h2>
          <p className="max-w-prose text-sm text-muted-foreground">
            {t("alertsSettings.alerts.recent.help", {
              limit: EVENTS_LIMIT,
            })}
          </p>
        </div>
        <AlertsList rows={eventRows} />
      </section>
    </div>
  );
}
