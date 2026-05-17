/**
 * /backtest/[id] — Backtest result viewer
 * Phase 0 skeleton: PnL curves and skip analysis come in Phase 2.
 */

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";

interface BacktestResultPageProps {
  params: Promise<{ id: string }>;
}

export default async function BacktestResultPage({
  params,
}: BacktestResultPageProps) {
  const { id } = await params;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-3xl font-bold">Backtest Result</h1>
        <p className="text-muted-foreground mt-1">Job ID: {id}</p>
      </div>

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>Job Status</CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-sm text-muted-foreground">
              Progress bar, status badge (queued/running/completed/failed), and
              result summary will appear here.
            </p>
            <Badge variant="outline" className="mt-3">
              Phase 2
            </Badge>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>PnL Curve (3 Scenarios)</CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-sm text-muted-foreground">
              Recharts LineChart comparing optimistic / neutral / pessimistic
              scenarios will appear here.
            </p>
            <Badge variant="outline" className="mt-3">
              Phase 2
            </Badge>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
