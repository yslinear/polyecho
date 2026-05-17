/**
 * /positions — Current open positions (Phase 3, read-only)
 *
 * Server Component: reads my_trades rows that are still open
 * (opened_at set, closed_at NULL) via Drizzle and renders them in a
 * shadcn table with a graceful empty state.
 */

import { and, isNull, isNotNull, desc } from "drizzle-orm";

import { db } from "@/lib/db";
import { myTrades } from "@/lib/db/schema";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  shortAddress,
  formatDateTime,
  formatSignedUsd,
  pnlColor,
  toNum,
} from "@/lib/format";

export const dynamic = "force-dynamic";

export type MyTradeRow = typeof myTrades.$inferSelect;

export default async function PositionsPage() {
  const rows: MyTradeRow[] = await db
    .select()
    .from(myTrades)
    .where(and(isNotNull(myTrades.opened_at), isNull(myTrades.closed_at)))
    .orderBy(desc(myTrades.opened_at));

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-3xl font-bold">Positions</h1>
        <p className="text-muted-foreground mt-1">
          Current open positions — {rows.length} open
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Open Positions</CardTitle>
        </CardHeader>
        <CardContent>
          {rows.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              No open positions. Trades with{" "}
              <code className="font-mono text-xs bg-muted px-1 py-0.5 rounded">
                closed_at IS NULL
              </code>{" "}
              will appear here once trader-core opens a copy position.
            </p>
          ) : (
            <div className="rounded-md border">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Market</TableHead>
                    <TableHead>Leader</TableHead>
                    <TableHead>Side</TableHead>
                    <TableHead className="text-right">Size</TableHead>
                    <TableHead className="text-right">Entry Price</TableHead>
                    <TableHead>Fill</TableHead>
                    <TableHead className="text-right">PnL</TableHead>
                    <TableHead>Opened</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {rows.map((r) => {
                    const size = toNum(r.size);
                    const price = toNum(r.price);
                    return (
                      <TableRow key={r.id}>
                        <TableCell
                          className="font-mono text-xs max-w-[180px] truncate"
                          title={r.market_id ?? undefined}
                        >
                          {r.market_id ?? "—"}
                        </TableCell>
                        <TableCell
                          className="font-mono text-xs"
                          title={r.leader_address ?? undefined}
                        >
                          {r.leader_address
                            ? shortAddress(r.leader_address)
                            : "—"}
                        </TableCell>
                        <TableCell>
                          {r.side ? (
                            <span
                              className={
                                r.side.toUpperCase() === "BUY"
                                  ? "text-xs font-semibold text-green-600 dark:text-green-400"
                                  : "text-xs font-semibold text-red-600 dark:text-red-400"
                              }
                            >
                              {r.side.toUpperCase()}
                            </span>
                          ) : (
                            "—"
                          )}
                        </TableCell>
                        <TableCell className="text-right tabular-nums text-sm">
                          {size === null
                            ? "—"
                            : size.toLocaleString("en-US")}
                        </TableCell>
                        <TableCell className="text-right tabular-nums text-sm">
                          {price === null ? "—" : price.toFixed(4)}
                        </TableCell>
                        <TableCell>
                          <Badge
                            variant={
                              r.fill_status === "filled"
                                ? "default"
                                : r.fill_status === "rejected"
                                  ? "destructive"
                                  : "secondary"
                            }
                            className="capitalize text-xs"
                          >
                            {r.fill_status}
                          </Badge>
                        </TableCell>
                        <TableCell
                          className={`text-right tabular-nums text-sm font-medium ${pnlColor(
                            r.pnl_usd
                          )}`}
                        >
                          {toNum(r.pnl_usd) === null
                            ? "—"
                            : formatSignedUsd(r.pnl_usd)}
                        </TableCell>
                        <TableCell className="text-xs text-muted-foreground whitespace-nowrap">
                          {formatDateTime(r.opened_at)}
                        </TableCell>
                      </TableRow>
                    );
                  })}
                </TableBody>
              </Table>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
