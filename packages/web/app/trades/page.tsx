/**
 * /trades — Trade history
 * Phase 0 skeleton: my_trades table with filters comes in Phase 3.
 */

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";

export default function TradesPage() {
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-3xl font-bold">Trades</h1>
        <p className="text-muted-foreground mt-1">
          All copy trades — fill status, PnL, timestamps
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Trade History</CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-sm text-muted-foreground">
            Paginated my_trades table with fill_status filter and CSV export
            will appear here.
          </p>
          <Badge variant="outline" className="mt-3">
            Phase 3
          </Badge>
        </CardContent>
      </Card>
    </div>
  );
}
