/**
 * /trades — Copy-trade history (Phase 3, read-only)
 *
 * Server Component: reads my_trades via Drizzle, passes rows to a client
 * island (TradesTable) that uses the shadcn data-table (TanStack) for
 * sorting, faceted fill_status filter and pagination.
 */

import { desc } from "drizzle-orm";

import { db } from "@/lib/db";
import { myTrades } from "@/lib/db/schema";
import { t } from "@/lib/i18n";
import { TradesTable } from "./trades-table";

export const dynamic = "force-dynamic";

// Drizzle-inferred row type (no `any`)
export type MyTradeRow = typeof myTrades.$inferSelect;

export default async function TradesPage() {
  const rows: MyTradeRow[] = await db
    .select()
    .from(myTrades)
    .orderBy(desc(myTrades.opened_at));

  return (
    <div className="space-y-8">
      <header className="space-y-1.5">
        <h1 className="text-2xl font-bold tracking-tight">
          {t("tables.trades.title")}
        </h1>
        <p className="text-sm text-muted-foreground">
          {rows.length === 0
            ? t("tables.trades.subtitleEmpty")
            : t("tables.trades.subtitleCount", { count: rows.length })}
        </p>
      </header>

      {rows.length === 0 ? (
        <div className="rounded-lg border border-dashed py-16 text-center">
          <p className="text-sm text-muted-foreground">
            {t("tables.trades.emptyTitle")}
          </p>
        </div>
      ) : (
        <TradesTable data={rows} />
      )}
    </div>
  );
}
