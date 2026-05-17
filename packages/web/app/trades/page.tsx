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
    <div className="space-y-6">
      <div>
        <h1 className="text-3xl font-bold">Trades</h1>
        <p className="text-muted-foreground mt-1">
          All copy trades — {rows.length} record{rows.length === 1 ? "" : "s"}
        </p>
      </div>

      <TradesTable data={rows} />
    </div>
  );
}
