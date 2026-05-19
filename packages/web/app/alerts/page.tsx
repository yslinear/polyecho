/**
 * /alerts — 警示中心 (PRD §5.8 + §5.7).
 *
 * Server Component. Reads (NO writes here; writes are in actions.ts behind
 * client islands):
 *  - recent `events` newest-first (severity, humanised type, payload summary,
 *    delivered? from pushed_at) -> client severity filter.
 *  - `vapid_public_key` settings row -> handed to the push opt-in island
 *    (PUBLIC key only; the private key never leaves p5-core).
 *
 * §5.7 dangerous ops use an INLINE single-step confirm (type-to-confirm +
 * danger passphrase in ONE authenticated Server Action that writes a
 * `confirmed` row trader-core executes). There is therefore no longer a
 * separate "awaiting confirmation" pending-row list to render here — the
 * operator decided that elaborate request-then-confirm UI is unnecessary.
 *
 * The web NEVER emits events or sends pushes — core/push own that. This page
 * only surfaces them and the opt-in / inline danger-confirm controls.
 */

import { desc } from "drizzle-orm";

import { db } from "@/lib/db";
import { events } from "@/lib/db/schema";
import { PushSubscribe } from "@/components/push-subscribe";
import { getVapidPublicKey } from "./actions";
import { AlertsList, type AlertEventRow } from "./severity-filter";
import { DangerousActionInline } from "./dangerous-action";
import { t } from "@/lib/i18n";

export const dynamic = "force-dynamic";

const EVENTS_LIMIT = 100;

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

        <DangerousActionInline />
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
