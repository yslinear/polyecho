/**
 * /status — System status (PRD §7.1 / §7.4).
 *
 * Read-only Server Component. It reads the worker heartbeat rows and the
 * integration self-probe row from the `settings` table via the existing
 * Drizzle client, parses them defensively through the FROZEN cross-language
 * coercers in lib/settings-keys (the EXACT pattern app/settings/page.tsx
 * uses for `trader_heartbeat`), and surfaces liveness + connectivity.
 *
 * Producers are the Python workers (`write_heartbeat` /
 * `write_integration_status`). The web NEVER writes these rows. Every value
 * is treated as possibly absent / legacy-wrapped / null: a bad row degrades
 * to an honest "No data / unavailable" state, never an exception
 * (PolyHermes "zero vs unavailable").
 *
 * Layout note: the two sections are deliberately DIFFERENT shapes (stacked
 * worker rows vs a compact probe list) — NOT an identical card grid, NO
 * hero-metric, NO glassmorphism (DESIGN.md absolute bans). Section spacing
 * is varied (10 / 6 / 4). Status is Badge variant + icon + text label,
 * never colour alone.
 */

import { inArray } from "drizzle-orm";

import { db } from "@/lib/db";
import { settings } from "@/lib/db/schema";
import {
  WORKER_HEARTBEAT_KEYS,
  INTEGRATION_STATUS_KEY,
  coerceHeartbeat,
  heartbeatLiveness,
  coerceIntegrationStatus,
  probeVerdict,
  HEARTBEAT_STALE_MINUTES,
  type IntegrationStatus,
} from "@/lib/settings-keys";
import {
  workerPresentation,
  probePresentation,
  integrationNoData,
  workerLabel,
  probeLabel,
} from "@/lib/status";
import { formatRelativeTime, formatDateTime } from "@/lib/format";
import { StatusBadge } from "@/components/status-badge";
import { Separator } from "@/components/ui/separator";
import { t } from "@/lib/i18n";

export const dynamic = "force-dynamic";

/** Worker counters we surface, in a stable order; the rest are ignored.
 *  Each entry pairs the heartbeat field with its i18n label key so the
 *  key is a statically-known I18nKey literal (no template-literal widening). */
const COUNTER_FIELDS = [
  ["detected", "status.workers.counters.detected"],
  ["copied", "status.workers.counters.copied"],
  ["skipped", "status.workers.counters.skipped"],
  ["placed", "status.workers.counters.placed"],
] as const;

export default async function StatusPage() {
  // Resilient read: a transient DB outage must not 500 the status page —
  // the whole point is to stay legible when things are degraded. Fall back
  // to "no rows" and let the honest empty/unavailable states speak.
  let rowError = false;
  let byKey = new Map<string, unknown>();
  try {
    const rows = await db
      .select({ key: settings.key, value: settings.value })
      .from(settings)
      .where(
        inArray(settings.key, [
          ...WORKER_HEARTBEAT_KEYS,
          INTEGRATION_STATUS_KEY,
        ])
      );
    byKey = new Map(rows.map((r) => [r.key, r.value]));
  } catch {
    rowError = true;
  }

  const now = new Date();

  const workers = WORKER_HEARTBEAT_KEYS.map((key) => {
    const hb = coerceHeartbeat(byKey.get(key));
    const liveness = heartbeatLiveness(hb, now);
    return { key, hb, presentation: workerPresentation(liveness) };
  });

  const hasIntegrationRow = byKey.has(INTEGRATION_STATUS_KEY);
  const integration: IntegrationStatus = coerceIntegrationStatus(
    byKey.get(INTEGRATION_STATUS_KEY)
  );
  const probeEntries = Object.entries(integration.probes).sort(([a], [b]) =>
    a.localeCompare(b)
  );

  return (
    <div className="space-y-10">
      <header className="space-y-1.5">
        <h1 className="text-2xl font-bold tracking-tight">
          {t("status.title")}
        </h1>
        <p className="max-w-prose text-sm text-muted-foreground">
          {t("status.intro")}
        </p>
        {rowError ? (
          <p
            role="alert"
            className="max-w-prose text-sm font-medium text-destructive"
          >
            {t("status.rowError")}
          </p>
        ) : null}
      </header>

      {/* ------------------------------------------------------------------ */}
      {/* Workers — stacked definition rows (intentionally NOT a card grid). */}
      {/* ------------------------------------------------------------------ */}
      <section aria-labelledby="workers-heading" className="space-y-4">
        <div className="space-y-1">
          <h2 id="workers-heading" className="text-base font-semibold">
            {t("status.workers.heading")}
          </h2>
          <p className="max-w-prose text-sm text-muted-foreground">
            {t("status.workers.staleHint", {
              minutes: HEARTBEAT_STALE_MINUTES,
            })}
          </p>
        </div>

        <div className="overflow-hidden rounded-lg border">
          {workers.map(({ key, hb, presentation }, i) => (
            <div key={key}>
              {i > 0 ? <Separator /> : null}
              <div className="flex flex-wrap items-start justify-between gap-x-6 gap-y-2 px-4 py-3.5">
                <div className="min-w-[8rem] space-y-0.5">
                  <div className="text-sm font-medium">
                    {workerLabel(key)}
                  </div>
                  <div className="text-xs text-muted-foreground">
                    {hb
                      ? t("status.workers.lastHeartbeat", {
                          time: formatRelativeTime(hb.ts, now),
                        })
                      : t("status.workers.noHeartbeat")}
                  </div>
                </div>

                <dl className="flex flex-1 flex-wrap items-start gap-x-6 gap-y-1 text-xs">
                  <div className="space-y-0.5">
                    <dt className="text-muted-foreground">
                      {t("status.workers.phase")}
                    </dt>
                    <dd className="font-medium">
                      {hb?.phase ?? t("status.workers.unknown")}
                    </dd>
                  </div>
                  <div className="space-y-0.5">
                    <dt className="text-muted-foreground">
                      {t("status.workers.datasource")}
                    </dt>
                    <dd className="font-medium">
                      {hb?.datasource ?? t("status.workers.unknown")}
                    </dd>
                  </div>
                  {COUNTER_FIELDS.map(([field, labelKey]) => {
                    const v = hb?.[field];
                    return (
                      <div key={field} className="space-y-0.5">
                        <dt className="text-muted-foreground">
                          {t(labelKey)}
                        </dt>
                        <dd className="font-medium tabular-nums">
                          {typeof v === "number" ? v : "—"}
                        </dd>
                      </div>
                    );
                  })}
                </dl>

                <StatusBadge
                  presentation={presentation}
                  className="shrink-0"
                />
              </div>
            </div>
          ))}
        </div>
      </section>

      {/* ------------------------------------------------------------------ */}
      {/* Integrations — compact probe list (a different shape on purpose).  */}
      {/* ------------------------------------------------------------------ */}
      <section aria-labelledby="integrations-heading" className="space-y-3">
        <div className="space-y-1">
          <h2 id="integrations-heading" className="text-base font-semibold">
            {t("status.integrations.heading")}
          </h2>
          <p className="max-w-prose text-sm text-muted-foreground">
            {t("status.integrations.lastProbeLead")}{" "}
            {integration.checked_at
              ? t("status.integrations.checkedAt", {
                  relative: formatRelativeTime(integration.checked_at, now),
                  absolute: formatDateTime(integration.checked_at),
                })
              : t("status.integrations.noProbeYet")}
          </p>
        </div>

        {!hasIntegrationRow || probeEntries.length === 0 ? (
          <div className="rounded-lg border border-dashed py-12 text-center">
            <p className="text-sm text-muted-foreground">
              {t("status.integrations.emptyState")}
            </p>
            {hasIntegrationRow ? (
              <StatusBadge
                presentation={integrationNoData()}
                className="mt-3"
              />
            ) : null}
          </div>
        ) : (
          <ul className="divide-y rounded-lg border">
            {probeEntries.map(([name, probe]) => (
              <li
                key={name}
                className="flex flex-wrap items-start justify-between gap-x-6 gap-y-2 px-4 py-3"
              >
                <div className="min-w-[10rem] space-y-0.5">
                  <div className="text-sm font-medium">
                    {probeLabel(name)}
                  </div>
                  <div className="max-w-prose break-words text-xs text-muted-foreground">
                    {probe.detail || t("status.integrations.noDetail")}
                  </div>
                </div>

                <div className="flex items-center gap-4 text-xs">
                  <div className="space-y-0.5 text-right">
                    <div className="text-muted-foreground">
                      {t("status.integrations.latency")}
                    </div>
                    <div className="font-medium tabular-nums">
                      {probe.latency_ms === null
                        ? "—"
                        : t("status.integrations.latencyMs", {
                            ms: probe.latency_ms,
                          })}
                    </div>
                  </div>
                  <div className="space-y-0.5 text-right">
                    <div className="text-muted-foreground">
                      {t("status.integrations.checked")}
                    </div>
                    <div className="font-medium tabular-nums">
                      {probe.checked_at
                        ? formatRelativeTime(probe.checked_at, now)
                        : t("status.integrations.never")}
                    </div>
                  </div>
                  <StatusBadge
                    presentation={probePresentation(probeVerdict(probe))}
                    className="shrink-0"
                  />
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
