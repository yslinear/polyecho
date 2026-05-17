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
        <h1 className="text-2xl font-bold tracking-tight">Settings</h1>
        <p className="max-w-prose text-sm text-muted-foreground">
          Global trading configuration. Changes are written through deliberate
          server actions; dangerous operations stay on the CLI.
        </p>
      </header>

      <section className="space-y-4">
        <div className="space-y-1">
          <h2 className="text-base font-semibold">Polymarket proxy</h2>
          <p className="max-w-prose text-sm text-muted-foreground">
            Outbound HTTP proxy for every Polymarket request. Leave it blank to
            connect directly.
          </p>
        </div>

        <ProxyForm
          current={current}
          lastUpdated={row?.updated_at ? row.updated_at.toISOString() : null}
        />
      </section>

      <section className="space-y-4 border-t pt-6">
        <div className="space-y-1">
          <h2 className="text-base font-semibold">旅行 / 保守模式</h2>
          <p className="max-w-prose text-sm text-muted-foreground">
            保守模式會自動收緊跟單比例與風險上限（PRD §5.6），適合外出或
            無法即時盯盤時使用。trader-core 即時讀取，立即生效，無需重啟。
          </p>
        </div>
        <RiskModeForm current={riskMode} />
      </section>

      <section className="space-y-4 border-t pt-6">
        <div className="space-y-1">
          <h2 className="text-base font-semibold">推播通知</h2>
          <p className="max-w-prose text-sm text-muted-foreground">
            在此裝置開啟 Web Push，重要事件即時送達。完整警示記錄請見{" "}
            <span className="font-medium text-foreground">警示中心</span>。
          </p>
        </div>
        <PushSubscribe vapidPublicKey={vapidPublicKey} />
      </section>

      <section className="space-y-3 border-t pt-6">
        <h2 className="text-base font-semibold">Copy templates &amp; risk</h2>
        <p className="max-w-prose text-sm text-muted-foreground">
          Conservative / Balanced / Aggressive templates, daily loss limits
          and the category whitelist are edited from the CLI for now. They
          will move here in a later phase.
        </p>
      </section>
    </div>
  );
}
