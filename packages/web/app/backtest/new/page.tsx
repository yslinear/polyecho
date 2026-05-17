/**
 * /backtest/new — Create a new backtest job
 * Phase 0 skeleton: the form and job submission come in Phase 2.
 */

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";

export default function NewBacktestPage() {
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-3xl font-bold">New Backtest</h1>
        <p className="text-muted-foreground mt-1">
          Configure and submit a backtest job
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Backtest Parameters</CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-sm text-muted-foreground">
            Form fields (leader address, date range, copy_ratio, slippage_bps,
            execution_delay_seconds) and three-scenario selector will appear
            here.
          </p>
          <Badge variant="outline" className="mt-3">
            Phase 2
          </Badge>
        </CardContent>
      </Card>
    </div>
  );
}
