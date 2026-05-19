/**
 * / — Dashboard overview (Phase 3, read-only).
 *
 * Information-led overview, NOT the banned SaaS hero-metric template and NOT
 * four identical stat cards. One honest focal block (today's realized +
 * unrealized PnL and current open risk) reading the real DB, then supporting
 * context (recent events, coverage) in a deliberately varied layout. Every
 * figure degrades to a calm, truthful empty/zero state — the fixture DB is
 * often genuinely empty (PRODUCT.md "Honest empty states").
 *
 * Server Component: reads via the existing Drizzle client. Schema unchanged —
 * my_trades has no dedicated unrealized column, so "unrealized" is honestly
 * the PnL on still-open positions and "realized" is PnL on closed ones.
 */

import { desc, isNull, and, isNotNull, eq } from "drizzle-orm";

import { Badge } from "@/components/ui/badge";
import { Pnl } from "@/components/pnl";
import { TraderHeartbeatStatus } from "@/components/trader-heartbeat-status";
import { db } from "@/lib/db";
import { myTrades, followRelations, events, settings } from "@/lib/db/schema";
import { t } from "@/lib/i18n";
import {
  TRADER_HEARTBEAT_KEY,
  coerceHeartbeat,
  heartbeatLiveness,
} from "@/lib/settings-keys";
import {
  toNum,
  sumPnl,
  sumPnlSince,
  startOfTodayUtc,
  formatDateTime,
  formatUsd,
} from "@/lib/format";

// Reads the database on every request — never prerender at build time
// (no DB is available during `next build` / CI).
export const dynamic = "force-dynamic";

const SEVERITY_VARIANT: Record<
  string,
  "default" | "secondary" | "outline" | "destructive"
> = {
  info: "secondary",
  warning: "outline",
  error: "destructive",
  critical: "destructive",
};

export default async function DashboardPage() {
  const [allTrades, openRows, following, latestEvents, heartbeatRows] =
    await Promise.all([
    db
      .select({
        pnl_usd: myTrades.pnl_usd,
        opened_at: myTrades.opened_at,
        closed_at: myTrades.closed_at,
      })
      .from(myTrades),
    db
      .select({
        pnl_usd: myTrades.pnl_usd,
        size: myTrades.size,
        price: myTrades.price,
        opened_at: myTrades.opened_at,
      })
      .from(myTrades)
      .where(and(isNotNull(myTrades.opened_at), isNull(myTrades.closed_at))),
    db
      .select({ id: followRelations.id, status: followRelations.status })
      .from(followRelations),
    db.select().from(events).orderBy(desc(events.created_at)).limit(8),
    db
      .select({ value: settings.value })
      .from(settings)
      .where(eq(settings.key, TRADER_HEARTBEAT_KEY))
      .limit(1),
  ]);

  // P1-5: bot liveness, read-only. The supervised loop upserts this row
  // every cycle; a missing/stale row means the engine is DOWN.
  const heartbeat = coerceHeartbeat(heartbeatRows[0]?.value);
  const liveness = heartbeatLiveness(heartbeat);

  const closed = allTrades.filter((t) => t.closed_at != null);
  const realizedPnl = sumPnl(closed.map((t) => t.pnl_usd));
  const unrealizedPnl = sumPnl(openRows.map((t) => t.pnl_usd));
  const todayPnl = sumPnlSince(
    allTrades.map((t) => ({ pnl_usd: t.pnl_usd, opened_at: t.opened_at })),
    startOfTodayUtc()
  );

  // Open risk = entry notional still on the table (size * price), not PnL.
  const openRisk = openRows.reduce((acc, r) => {
    const size = toNum(r.size);
    const price = toNum(r.price);
    if (size === null || price === null) return acc;
    return acc + Math.abs(size * price);
  }, 0);

  const openPositions = openRows.length;
  const activeFollows = following.filter((f) => f.status === "active").length;
  const hasAnyTrades = allTrades.length > 0;

  return (
    <div className="mx-auto max-w-6xl">
      <header className="mb-8">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <h1 className="text-2xl font-bold tracking-tight">
            {t("dashboard.title")}
          </h1>
          <TraderHeartbeatStatus
            heartbeat={heartbeat}
            liveness={liveness}
          />
        </div>
        <p className="mt-1 text-sm text-muted-foreground">
          {t("dashboard.subtitle")}
        </p>
      </header>

      {/* FOCAL BLOCK — a single PnL ledger strip, not a hero number and not
          a row of identical cards. Today's net is the lead; realized,
          unrealized and open risk sit beside it as a connected reading. */}
      <section
        aria-label={t("dashboard.summaryAriaLabel")}
        className="rounded-lg border bg-card"
      >
        <div className="grid gap-px overflow-hidden rounded-lg bg-border sm:grid-cols-[1.4fr_1fr_1fr_1fr]">
          <div className="bg-card p-6">
            <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
              {t("dashboard.todayPnl.label")}
            </p>
            <Pnl
              value={hasAnyTrades ? todayPnl : null}
              emphasis="metric"
              className="mt-2 block text-3xl"
            />
            <p className="mt-2 text-xs text-muted-foreground">
              {t("dashboard.todayPnl.caption")}
            </p>
          </div>

          <div className="bg-card p-6">
            <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
              {t("dashboard.realized.label")}
            </p>
            <Pnl
              value={closed.length > 0 ? realizedPnl : null}
              className="mt-2 block text-xl"
            />
            <p className="mt-2 text-xs text-muted-foreground">
              {t("dashboard.realized.caption", { count: closed.length })}
            </p>
          </div>

          <div className="bg-card p-6">
            <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
              {t("dashboard.unrealized.label")}
            </p>
            <Pnl
              value={openPositions > 0 ? unrealizedPnl : null}
              className="mt-2 block text-xl"
            />
            <p className="mt-2 text-xs text-muted-foreground">
              {t("dashboard.unrealized.caption", { count: openPositions })}
            </p>
          </div>

          <div className="bg-card p-6">
            <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
              {t("dashboard.openRisk.label")}
            </p>
            <p className="mt-2 text-xl font-semibold tabular-nums">
              {openPositions > 0 ? (
                formatUsd(openRisk)
              ) : (
                <span className="text-muted-foreground">$0.00</span>
              )}
            </p>
            <p className="mt-2 text-xs text-muted-foreground">
              {t("dashboard.openRisk.caption")}
            </p>
          </div>
        </div>
        {!hasAnyTrades && (
          <p className="border-t px-6 py-3 text-sm text-muted-foreground">
            {t("dashboard.noTrades")}
          </p>
        )}
      </section>

      {/* SUPPORTING — intentionally NOT a uniform grid. A wide activity feed
          carries the weight; a narrow coverage rail sits beside it. */}
      <div className="mt-8 grid gap-5 lg:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
        <section aria-labelledby="activity-heading">
          <div className="mb-3 flex items-baseline justify-between">
            <h2
              id="activity-heading"
              className="text-sm font-semibold tracking-tight"
            >
              {t("dashboard.activity.heading")}
            </h2>
            <span className="text-xs text-muted-foreground">
              {t("dashboard.activity.latest", {
                count: Math.min(latestEvents.length, 8) || 0,
              })}
            </span>
          </div>
          <div className="rounded-lg border bg-card">
            {latestEvents.length === 0 ? (
              <p className="px-5 py-8 text-center text-sm text-muted-foreground">
                {t("dashboard.activity.empty")}
              </p>
            ) : (
              <ul className="divide-y">
                {latestEvents.map((e) => (
                  <li
                    key={e.id}
                    className="flex items-center justify-between gap-3 px-5 py-3"
                  >
                    <div className="flex min-w-0 items-center gap-2.5">
                      <Badge
                        variant={SEVERITY_VARIANT[e.severity] ?? "secondary"}
                        className="shrink-0 capitalize"
                      >
                        {e.severity}
                      </Badge>
                      <span className="truncate text-sm font-medium">
                        {e.event_type}
                      </span>
                    </div>
                    <time className="shrink-0 whitespace-nowrap text-xs tabular-nums text-muted-foreground">
                      {formatDateTime(e.created_at)}
                    </time>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </section>

        <section aria-labelledby="coverage-heading" className="space-y-3">
          <h2
            id="coverage-heading"
            className="text-sm font-semibold tracking-tight"
          >
            {t("dashboard.coverage.heading")}
          </h2>
          <dl className="rounded-lg border bg-card">
            <div className="flex items-center justify-between px-5 py-4">
              <dt className="text-sm text-muted-foreground">
                {t("dashboard.coverage.leadersFollowed")}
              </dt>
              <dd className="text-base font-semibold tabular-nums">
                {activeFollows}
                {following.length !== activeFollows && (
                  <span className="ml-1.5 text-xs font-normal text-muted-foreground">
                    / {following.length}
                  </span>
                )}
              </dd>
            </div>
            <div className="flex items-center justify-between border-t px-5 py-4">
              <dt className="text-sm text-muted-foreground">
                {t("dashboard.coverage.openPositions")}
              </dt>
              <dd className="text-base font-semibold tabular-nums">
                {openPositions}
              </dd>
            </div>
          </dl>
          {following.length === 0 && (
            <p className="text-xs text-muted-foreground">
              {t("dashboard.coverage.notFollowing")}
            </p>
          )}
        </section>
      </div>
    </div>
  );
}
