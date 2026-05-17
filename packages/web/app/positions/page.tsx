/**
 * /positions — Real-time open positions
 * Phase 0 skeleton: live positions come in Phase 3/4.
 */

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";

export default function PositionsPage() {
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-3xl font-bold">Positions</h1>
        <p className="text-muted-foreground mt-1">
          Current open positions (mark-to-market)
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Open Positions</CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-sm text-muted-foreground">
            Live open positions derived from my_trades (opened_at set,
            closed_at null) with unrealised PnL will appear here.
          </p>
          <Badge variant="outline" className="mt-3">
            Phase 3
          </Badge>
        </CardContent>
      </Card>
    </div>
  );
}
