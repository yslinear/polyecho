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
import { t } from "@/lib/i18n";
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
        <h1 className="text-2xl font-bold tracking-tight">
          {t("tables.positions.title")}
        </h1>
        <p className="text-sm text-muted-foreground">
          {rows.length === 0
            ? t("tables.positions.subtitleEmpty")
            : t("tables.positions.subtitleCount", { count: rows.length })}
        </p>
      </header>

      {rows.length === 0 ? (
        <div className="rounded-lg border border-dashed py-16 text-center">
          <p className="text-sm text-muted-foreground">
            {t("tables.positions.emptyTitle")}
          </p>
        </div>
      ) : (
        <PositionsTable data={rows} />
      )}
    </div>
  );
}
