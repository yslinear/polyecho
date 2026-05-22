/**
 * /settings — Global configuration (PRD §6.1).
 *
 * Server Component: reads the current `polymarket_proxy_url` settings row
 * via the Drizzle client and hands the value to the proxy form (client
 * island). trader-core's `_coerce_proxy` accepts a bare string OR
 * `{"value": "..."}`; we read either shape and normalise to a plain string.
 */

import { inArray } from "drizzle-orm";

import { db } from "@/lib/db";
import { settings } from "@/lib/db/schema";
import {
  RISK_MODE_KEY,
  VAPID_PUBLIC_KEY,
  coerceStringSetting,
  coerceRiskMode,
} from "@/lib/settings-keys";
import { PushSubscribe } from "@/components/push-subscribe";
import { t } from "@/lib/i18n";
import { ProxyForm } from "./proxy-form";
import { RiskModeForm } from "./risk-mode-form";

export const dynamic = "force-dynamic";

const SETTINGS_KEY = "polymarket_proxy_url";

export default async function SettingsPage() {
  const rows = await db
    .select({ key: settings.key, value: settings.value, updated_at: settings.updated_at })
    .from(settings)
    .where(
      inArray(settings.key, [SETTINGS_KEY, RISK_MODE_KEY, VAPID_PUBLIC_KEY])
    );

  const byKey = new Map(rows.map((r) => [r.key, r]));
  const row = byKey.get(SETTINGS_KEY);
  const current = coerceStringSetting(row?.value);
  const riskMode = coerceRiskMode(byKey.get(RISK_MODE_KEY)?.value);
  const vapidPublicKey = coerceStringSetting(
    byKey.get(VAPID_PUBLIC_KEY)?.value
  ).trim();

  return (
    <div className="mx-auto max-w-2xl space-y-8">
      <header className="space-y-1.5">
        <h1 className="text-2xl font-bold tracking-tight">
          {t("alertsSettings.settings.title")}
        </h1>
        <p className="max-w-prose text-sm text-muted-foreground">
          {t("alertsSettings.settings.intro")}
        </p>
      </header>

      <section className="space-y-4">
        <div className="space-y-1">
          <h2 className="text-base font-semibold">
            {t("alertsSettings.settings.proxy.heading")}
          </h2>
          <p className="max-w-prose text-sm text-muted-foreground">
            {t("alertsSettings.settings.proxy.help")}
          </p>
        </div>

        <ProxyForm
          current={current}
          lastUpdated={row?.updated_at ? row.updated_at.toISOString() : null}
        />
      </section>

      <section className="space-y-4 border-t pt-6">
        <div className="space-y-1">
          <h2 className="text-base font-semibold">
            {t("alertsSettings.settings.riskMode.heading")}
          </h2>
          <p className="max-w-prose text-sm text-muted-foreground">
            {t("alertsSettings.settings.riskMode.help")}
          </p>
        </div>
        <RiskModeForm current={riskMode} />
      </section>

      <section className="space-y-4 border-t pt-6">
        <div className="space-y-1">
          <h2 className="text-base font-semibold">
            {t("alertsSettings.settings.push.heading")}
          </h2>
          <p className="max-w-prose text-sm text-muted-foreground">
            {t("alertsSettings.settings.push.helpPrefix")}{" "}
            <span className="font-medium text-foreground">
              {t("alertsSettings.settings.push.helpLink")}
            </span>
            {t("alertsSettings.settings.push.helpSuffix")}
          </p>
        </div>
        <PushSubscribe vapidPublicKey={vapidPublicKey} />
      </section>

      <section className="space-y-3 border-t pt-6">
        <h2 className="text-base font-semibold">
          {t("alertsSettings.settings.copyTemplates.heading")}
        </h2>
        <p className="max-w-prose text-sm text-muted-foreground">
          {t("alertsSettings.settings.copyTemplates.help")}
        </p>
        <div className="rounded-lg border border-dashed px-4 py-6 text-center">
          <p className="text-sm text-muted-foreground">
            {t("alertsSettings.settings.copyTemplates.emptyState")}
          </p>
        </div>
      </section>
    </div>
  );
}
