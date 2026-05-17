/**
 * /positions — Current open positions (read-only).
 *
 * Server Component: reads my_trades rows still open (opened_at set,
 * closed_at NULL) and renders them in a consistent table with a calm,
 * honest empty state. Money goes through <Pnl/> (DESIGN.md).
 */

import { and, isNull, isNotNull, desc } from "drizzle-orm";

import { db } from "@/lib/db";
import { myTrades } from "@/lib/db/schema";
import { Badge } from "@/components/ui/badge";
import { Pnl } from "@/components/pnl";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { shortAddress, formatDateTime, toNum } from "@/lib/format";

export const dynamic = "force-dynamic";

export type MyTradeRow = typeof myTrades.$inferSelect;

const FILL_VARIANT: Record<
  string,
  "default" | "secondary" | "outline" | "destructive"
> = {
  filled: "default",
  pending: "secondary",
  partial: "outline",
  rejected: "destructive",
};

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
        <div className="overflow-hidden rounded-lg border">
          <Table>
            <TableHeader className="sticky top-0 z-10 bg-muted/60 backdrop-blur supports-[backdrop-filter]:bg-muted/60">
              <TableRow className="hover:bg-transparent">
                <TableHead>Market</TableHead>
                <TableHead>Leader</TableHead>
                <TableHead>Side</TableHead>
                <TableHead className="text-right">Size</TableHead>
                <TableHead className="text-right">Entry</TableHead>
                <TableHead>Fill</TableHead>
                <TableHead className="text-right">PnL</TableHead>
                <TableHead className="text-right">Opened</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((r) => {
                const size = toNum(r.size);
                const price = toNum(r.price);
                const buy = r.side?.toUpperCase() === "BUY";
                return (
                  <TableRow key={r.id} className="hover:bg-muted/50">
                    <TableCell
                      className="max-w-[200px] truncate font-mono text-xs"
                      title={r.market_id ?? undefined}
                    >
                      {r.market_id ?? "—"}
                    </TableCell>
                    <TableCell
                      className="font-mono text-xs"
                      title={r.leader_address ?? undefined}
                    >
                      {r.leader_address ? shortAddress(r.leader_address) : "—"}
                    </TableCell>
                    <TableCell>
                      {r.side ? (
                        <Badge
                          variant={buy ? "default" : "secondary"}
                          className="text-xs"
                        >
                          {r.side.toUpperCase()}
                        </Badge>
                      ) : (
                        <span className="text-muted-foreground">—</span>
                      )}
                    </TableCell>
                    <TableCell className="text-right tabular-nums text-sm">
                      {size === null ? "—" : size.toLocaleString("en-US")}
                    </TableCell>
                    <TableCell className="text-right tabular-nums text-sm">
                      {price === null ? "—" : price.toFixed(4)}
                    </TableCell>
                    <TableCell>
                      <Badge
                        variant={FILL_VARIANT[r.fill_status] ?? "secondary"}
                        className="capitalize text-xs"
                      >
                        {r.fill_status}
                      </Badge>
                    </TableCell>
                    <TableCell className="text-right">
                      {toNum(r.pnl_usd) === null ? (
                        <span className="text-sm text-muted-foreground">—</span>
                      ) : (
                        <Pnl value={r.pnl_usd} className="text-sm" />
                      )}
                    </TableCell>
                    <TableCell className="whitespace-nowrap text-right text-xs text-muted-foreground tabular-nums">
                      {formatDateTime(r.opened_at)}
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </div>
      )}
    </div>
  );
}
