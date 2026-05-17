/**
 * / — Dashboard overview (Phase 3, read-only)
 *
 * Server Component: reads my_trades, follow_relations and events via Drizzle
 * and renders summary cards + latest alerts. No fake data — every metric
 * degrades to a graceful empty/zero state when the DB has no rows.
 */

import { desc, isNull, and, isNotNull } from "drizzle-orm";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { db } from "@/lib/db";
import { myTrades, followRelations, events } from "@/lib/db/schema";
import {
  formatSignedUsd,
  pnlColor,
  sumPnl,
  sumPnlSince,
  startOfTodayUtc,
  formatDateTime,
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
  const [trades, openCount, following, latestEvents] = await Promise.all([
    db
      .select({
        pnl_usd: myTrades.pnl_usd,
        opened_at: myTrades.opened_at,
      })
      .from(myTrades),
    db
      .select({ id: myTrades.id })
      .from(myTrades)
      .where(and(isNotNull(myTrades.opened_at), isNull(myTrades.closed_at))),
    db
      .select({ id: followRelations.id, status: followRelations.status })
      .from(followRelations),
    db.select().from(events).orderBy(desc(events.created_at)).limit(10),
  ]);

  const totalPnl = sumPnl(trades.map((t) => t.pnl_usd));
  const todayPnl = sumPnlSince(trades, startOfTodayUtc());
  const openPositions = openCount.length;
  const activeFollows = following.filter((f) => f.status === "active").length;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-3xl font-bold">Dashboard</h1>
        <p className="text-muted-foreground mt-1">
          Copy-trading overview — read-only
        </p>
      </div>

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              Today&apos;s PnL
            </CardTitle>
          </CardHeader>
          <CardContent>
            <p className={`text-2xl font-bold ${pnlColor(todayPnl)}`}>
              {formatSignedUsd(todayPnl)}
            </p>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              Total PnL
            </CardTitle>
          </CardHeader>
          <CardContent>
            <p className={`text-2xl font-bold ${pnlColor(totalPnl)}`}>
              {formatSignedUsd(totalPnl)}
            </p>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              Open Positions
            </CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-2xl font-bold">{openPositions}</p>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              Following Leaders
            </CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-2xl font-bold">{activeFollows}</p>
            <p className="text-xs text-muted-foreground mt-1">
              {following.length} total relation
              {following.length === 1 ? "" : "s"}
            </p>
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Latest Alerts</CardTitle>
        </CardHeader>
        <CardContent>
          {latestEvents.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              No events yet. Alerts from trader-core (fills, errors, risk
              limits) will appear here.
            </p>
          ) : (
            <ul className="divide-y">
              {latestEvents.map((e) => (
                <li
                  key={e.id}
                  className="flex items-center justify-between gap-3 py-2 first:pt-0 last:pb-0"
                >
                  <div className="flex items-center gap-2 min-w-0">
                    <Badge
                      variant={SEVERITY_VARIANT[e.severity] ?? "secondary"}
                      className="capitalize shrink-0"
                    >
                      {e.severity}
                    </Badge>
                    <span className="text-sm font-medium truncate">
                      {e.event_type}
                    </span>
                  </div>
                  <span className="text-xs text-muted-foreground whitespace-nowrap shrink-0">
                    {formatDateTime(e.created_at)}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
