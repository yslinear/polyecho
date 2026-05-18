/**
 * /positions — Current open positions (read-only).
 *
 * Server Component: reads my_trades rows still open (opened_at set,
 * closed_at NULL) and hands them to the PositionsTable client island,
 * which uses the shared shadcn DataTable — the SAME instrument idiom as
 * /trades and /leaders (DESIGN.md). Money goes through <Pnl/>; the
 * genuinely-empty dataset gets a calm sentence + the real next action.
 */

import { and, isNull, isNotNull, desc } from "drizzle-orm";

import { db } from "@/lib/db";
import { myTrades } from "@/lib/db/schema";
import { PositionsTable } from "./positions-table";

export const dynamic = "force-dynamic";

export type MyTradeRow = typeof myTrades.$inferSelect;

export default async function PositionsPage() {
  const rows: MyTradeRow[] = await db
    .select()
    .from(myTrades)
    .where(and(isNotNull(myTrades.opened_at), isNull(myTrades.closed_at)))
    .orderBy(desc(myTrades.opened_at));

  return (
    <div className="space-y-8">
      <header className="space-y-1.5">
        <h1 className="text-2xl font-bold tracking-tight">Positions</h1>
        <p className="text-sm text-muted-foreground">
          {rows.length === 0
            ? "Open copy positions appear here in real time."
            : `${rows.length} open position${rows.length === 1 ? "" : "s"}.`}
        </p>
      </header>

      {rows.length === 0 ? (
        <div className="rounded-lg border border-dashed py-16 text-center">
          <p className="text-sm text-muted-foreground">
            No open positions. They appear here once trader-core opens a copy
            trade for a followed leader.
          </p>
        </div>
      ) : (
        <PositionsTable data={rows} />
      )}
    </div>
  );
}
