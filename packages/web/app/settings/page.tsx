/**
 * /settings — Global configuration (PRD §6.1).
 *
 * Server Component: reads the current `polymarket_proxy_url` settings row
 * via the Drizzle client and hands the value to the proxy form (client
 * island). trader-core's `_coerce_proxy` accepts a bare string OR
 * `{"value": "..."}`; we read either shape and normalise to a plain string.
 */

import { eq } from "drizzle-orm";

import { db } from "@/lib/db";
import { settings } from "@/lib/db/schema";
import { ProxyForm } from "./proxy-form";

export const dynamic = "force-dynamic";

const SETTINGS_KEY = "polymarket_proxy_url";

/** Normalise the stored jsonb (string | {value} | null) to a plain string. */
function coerceStored(value: unknown): string {
  if (typeof value === "string") return value;
  if (
    value &&
    typeof value === "object" &&
    "value" in value &&
    typeof (value as { value: unknown }).value === "string"
  ) {
    return (value as { value: string }).value;
  }
  return "";
}

export default async function SettingsPage() {
  const [row] = await db
    .select({ value: settings.value, updated_at: settings.updated_at })
    .from(settings)
    .where(eq(settings.key, SETTINGS_KEY))
    .limit(1);

  const current = coerceStored(row?.value);

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

      <section className="space-y-3 border-t pt-6">
        <h2 className="text-base font-semibold">Copy templates &amp; risk</h2>
        <p className="max-w-prose text-sm text-muted-foreground">
          Conservative / Balanced / Aggressive templates, the global pause
          toggle, daily loss limits and the category whitelist are edited from
          the CLI for now. They will move here in a later phase.
        </p>
      </section>
    </div>
  );
}
